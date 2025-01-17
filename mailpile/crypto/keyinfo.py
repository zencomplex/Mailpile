from __future__ import print_function
import time
import traceback

import pgpdump
import pgpdump.packet
from pgpdump.utils import PgpdumpException, get_int4
from mailpile.util import dict_merge


# Patch pgpdump so it stops crashing on weird public keys #####################

def monkey_patch_pgpdump():
    # Add Algorithm 22 to the lookup table
    pgpdump.packet.AlgoLookup.pub_algorithms[22] = 'EdDSA'

    # Patch the key parser to just silently ignore strange keys
    orig_pkm = pgpdump.packet.PublicKeyPacket.parse_key_material

    def _patched_pkm(self, offset):
        try:
            return orig_pkm(self, offset)
        except PgpdumpException:
            return offset
    pgpdump.packet.PublicKeyPacket.parse_key_material = _patched_pkm


# FIXME: Perhaps we should be checking pgpdump versions? But most of
#        these are actually API changes, not just bugfixes. It's likely
# that versions 1.6+ will continue to throw exceptsions on "unknown" key
# types... if/when 1.6 or 2.x get released, we'll just have to revisit
# this logic.
monkey_patch_pgpdump()


# Classes for storing PGP key info ############################################

class RestrictedDict(dict):
    KEYS = {}

    @classmethod
    def prep_properties(cls):
        def mk_prop(k):
            return property(lambda s: s[k], lambda s, v: s.__setitem__(k, v))
        for k in cls.KEYS:
            setattr(cls, k, mk_prop(k))

    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        for k, (t, d) in self.KEYS.items():
            if t in (list, dict):
                self[k] = t()

    def keys(self):
        kl = list(dict.keys(self))
        for dk in (k for k in self.KEYS if k not in kl):
            kl.append(dk)
        return sorted(kl)

    def __setitem__(self, item, value):
        if item[:1] != '_':
            if item not in self.KEYS:
                raise KeyError('Invalid key: %s' % item)
            if not isinstance(value, self.KEYS[item][0]):
                raise TypeError('Bad type for %s: %s (want %s)'
                                % (item, value, self.KEYS[item][0].__name__))
        dict.__setitem__(self, item, value)

    def __getitem__(self, item):
        if item[:1] == '_':
            return dict.__getitem__(self, item)
        else:
            return dict.get(self, item, self.KEYS[item][1])


class KeyUID(RestrictedDict):
    KEYS = {
        'name':    (str, ''),
        'email':   (str, ''),
        'comment': (str, '')}

    def __repr__(self):
        parts = []
        if self['name']:
            parts.append(self['name'])
        if self['email']:
            parts.append('<%s>' % self['email'])
        if self['comment']:
            parts.append('(%s)' % self['comment'])
        return ' '.join(parts)


class KeyInfo(RestrictedDict):
    KEYS = {
        'fingerprint':  (str, 'MISSING'),
        'capabilities': (str, ''),
        'keytype_name': (str, 'unknown'),
        'keytype_code': (int, 0),
        'keysize':      (int, 0),
        'created':      (int, 0),
        'expires':      (int, 0),
        'validity':     (str, '?'),
        'uids':         (list, None),
        'subkeys':      (list, None),
        'is_subkey':    (bool, False),
        'on_keychain':  (bool, False)}

    expired = property(lambda k: time.time() > k.expires > 0)

    is_usable = property(lambda k: k.validity in ('', '?') and not k.expired)

    can_encrypt = property(lambda k: 'e' in k.capabilities and k.is_usable)

    can_sign = property(lambda k: 's' in k.capabilities and k.is_usable)

    def summary(self, full_fingerprint=False):
        """
        Generate a short string summarizing the key's main properties: key ID,
        UIDs, expiration date, algorithm, size, capabilities, and validity.

        Note: If summary ends with !, the key is invalid/unusable.
        """
        now = time.time()
        emails = ','.join([u.email for u in self.uids if u.email])
        return '%s%s%s/%s%s/%s%s' % (
            self.fingerprint[-(9999 if full_fingerprint else 16):],
            ('=%s' % emails) if emails else '',
            ('<%x' % self.expires) if self.expires else '',
            self.keytype_name[:3],
            self.keysize,
            self.capabilities,
            ('' if self.is_usable else '!'))

    def __repr__(self):
        if self.is_subkey:
            return self.summary()
        return '{ %s }' % '\n  '.join(
            '%-12s = %s' % (k, self[k])
            for k in self.keys() if self[k] is not None)

    def ensure_autocrypt_uid(keyinfo, ac_uid):
        """Ensure we include the email from the Autocrypt header in a UID."""
        if keyinfo.is_subkey:
            return
        found = 0
        for uid in keyinfo.uids:
            if uid.email == ac_uid.email:
                uid.comment = uid.comment + '(Autocrypt)'
                found += 1
        if not found:
            keyinfo.uids += [ac_uid]

    def add_subkey_capabilities(keyinfo, now=None):
        """Make key "inherit" the capabilities of any un-expired subkeys."""
        now = now or time.time()
        subkey_caps = set()
        for subkey in keyinfo.subkeys:
            if not (0 < subkey.expires < now):
                subkey_caps |= set(c for c in subkey.capabilities)
        if subkey_caps:
            keyinfo.capabilities = '%s+%s' % (
                keyinfo.capabilities.split('+')[0],
                ''.join(sorted(list(subkey_caps))))

    def synthesize_validity(keyinfo, now=None):
        """Synthesize key validity property."""
        # FIXME: Revocations?
        now = now or time.time()
        if 0 < keyinfo.expires < now and keyinfo.validity in ('', '?'):
            keyinfo.validity = 'e'


class MailpileKeyInfo(KeyInfo):
    KEYS = dict_merge(KeyInfo.KEYS, {
        'vcards':       (dict, None),
        'origins':      (list, None),
        'scores':       (dict, None),
        'score_stars':  (int, 0),
        'score_reason': (unicode, None),
        'score':        (int, 0)})


KeyUID.prep_properties()
KeyInfo.prep_properties()
MailpileKeyInfo.prep_properties()


def get_keyinfo(data, autocrypt_header=None,
                key_info_class=KeyInfo, key_uid_class=KeyUID):
    """
    This method will parse a stream of OpenPGP packets into a list of KeyInfo
    objects.

    Note: Signatures are not validated, this code only parses the data.
    """
    try:
        if "-----BEGIN" in data:
            ak = pgpdump.AsciiData(data)
        else:
            ak = pgpdump.BinaryData(data)
        packets = list(ak.packets())
    except (TypeError, IndexError, PgpdumpException):
        traceback.print_exc()
        return []

    def _unixtime(packet, seconds=0, days=0):
        return (packet.raw_creation_time
                + (days or 0) * 24 * 3600
                + (seconds or 0))

    results = []
    last_uid = key_uid_class()  # Dummy
    last_key = key_info_class()  # Dummy
    for m in packets:
        try:
            if isinstance(m, pgpdump.packet.PublicKeyPacket):
                size = str(int(1.024 *
                               round(len('%x' % (m.modulus or 0)) / 0.256)))
                last_key = key_info_class(
                    fingerprint=m.fingerprint,
                    keytype_name=m.pub_algorithm or '',
                    keytype_code=m.raw_pub_algorithm,
                    keysize=size)
                if isinstance(m, pgpdump.packet.PublicSubkeyPacket):
                    last_key.is_subkey = True
                    results[-1].subkeys.append(last_key)
                else:
                    results.append(last_key)

                # Older pgpdumps may fail here and cause traceback noise, but
                # the loop will limp onwards.
                last_key.created = _unixtime(m)
                if m.raw_days_valid > 0:
                    last_key.expires = _unixtime(m, days=m.raw_days_valid)
                    if last_key.expires == last_key.created:
                        last_key.expires = 0

            elif isinstance(m, pgpdump.packet.UserIDPacket) and results:
                last_uid = key_uid_class(name=m.user_name, email=m.user_email)
                last_key.uids.append(last_uid)

            elif isinstance(m, pgpdump.packet.SignaturePacket) and results:
                for s in m.subpackets:
                    if s.subtype == 9:
                        exp = _unixtime(m, seconds=get_int4(s.data, 0))
                        if 0 < exp < last_key.expires or 0 == last_key.expires:
                            last_key.expires = exp
                    elif s.subtype == 27:
                        caps = set(c for c in last_key.capabilities)
                        for flag, c in ((0x01, 'c'), (0x02, 's'),
                                        (0x0C, 'e'), (0x20, 'a')):
                            if s.data[0] & flag:
                                caps.add(c)
                        last_key.capabilities = ''.join(caps)

        except (TypeError, AttributeError, KeyError, IndexError, NameError):
            traceback.print_exc()

    autocrypt_uid = None
    if autocrypt_header:
        # The autocrypt spec tells us that the visible addr= attribute
        # overrides whatever is on the key itself, so we synthesize a
        # fake UID here so the info is correct in an Autocrypt context.
        autocrypt_uid = key_uid_class(
            email=autocrypt_header['addr'],
            comment='Autocrypt')

    now = time.time()
    for keyinfo in results:
        keyinfo.synthesize_validity(now=now)
        keyinfo.add_subkey_capabilities(now=now)
        if autocrypt_uid is not None:
            keyinfo.ensure_autocrypt_uid(autocrypt_uid)

    return results


if __name__ == "__main__":
    import sys

    for f in sys.argv[1:]:
        with open(f, 'r') as fd:
            keyinfo = get_keyinfo(fd.read())[0]
            print('%s' % keyinfo)
            print('%s' % keyinfo.summary(full_fingerprint=True))
            print('Is usable = %s, Can encrypt = %s, Can sign = %s' % (
                keyinfo.is_usable, keyinfo.can_encrypt, keyinfo.can_sign))
            print('')

# EOF
