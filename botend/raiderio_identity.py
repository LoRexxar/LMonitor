"""Exact activity identity shared by persistence and lookups.

Realm slugs must already come from the canonical realm resolver. Hashing the
UTF-8 tuple avoids MySQL's accent-insensitive character-name comparisons.
"""
import hashlib
import json
import unicodedata


def normalized_name(value):
    return unicodedata.normalize('NFC', value.strip()).casefold()


def activity_identity_digest(region, realm_slug, character_name):
    identity = [region.strip().lower(), realm_slug.strip().lower(), normalized_name(character_name)]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
