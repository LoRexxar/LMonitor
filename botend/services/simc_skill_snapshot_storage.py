"""Packet-bounded storage for the two lossless snapshot actor documents.

Only explicit document reads materialize a complete JSON value. Part reads use
small ordinal windows, not iterator(): MySQL may buffer an iterator's full SQL
result. The actor's inline hero-tree header is a derived lookup index, never a
replacement for the original document stored in its parts.
"""

import hashlib
import json
import re
from itertools import chain

from django.db import router, transaction
from django.db.models import Count, Max, Min

from botend.models import SimcSkillDamageSnapshotActor, SimcSkillDamageSnapshotPart


class SkillSnapshotActorStorage:
    FORMAT = 'json_text_parts_v1'
    PART_CHARACTERS = 262144
    READ_PARTS = 4
    DOCUMENTS = ('actor_payload', 'unresolved_payload')

    @classmethod
    def _iter_json_parts(cls, value):
        encoder = json.JSONEncoder(
            ensure_ascii=False, separators=(',', ':'), allow_nan=False,
        )
        fragments = []
        length = 0
        for token in encoder.iterencode(value):
            offset = 0
            while offset < len(token):
                take = min(cls.PART_CHARACTERS - length, len(token) - offset)
                fragments.append(token[offset:offset + take])
                length += take
                offset += take
                if length == cls.PART_CHARACTERS:
                    yield ''.join(fragments)
                    fragments = []
                    length = 0
        if fragments:
            yield ''.join(fragments)

    @classmethod
    def create_actor(cls, **kwargs):
        """Accept Actor.objects.create kwargs; atomically persist bounded parts.

        Each document fitting in one part retains its original inline JSONField.
        Large documents are never passed to an actor INSERT or UPDATE. Individual
        part INSERTs deliberately avoid an unbounded bulk_create SQL statement.
        """
        if kwargs.get('payload_storage'):
            raise ValueError('payload_storage is managed by SkillSnapshotActorStorage')
        values = {
            'actor_payload': kwargs.get('actor_payload', {}),
            'unresolved_payload': kwargs.get('unresolved_payload', []),
        }
        pending = {}
        for document, value in values.items():
            parts = cls._iter_json_parts(value)
            first = next(parts)
            second = next(parts, None)
            if second is not None:
                pending[document] = chain((first, second), parts)
                if document == 'actor_payload':
                    header = {}
                    if isinstance(value, dict) and 'hero_talent_trees' in value:
                        header['hero_talent_trees'] = value['hero_talent_trees']
                    # The derived header must itself remain packet-bounded.
                    header_parts = cls._iter_json_parts(header)
                    next(header_parts)
                    if next(header_parts, None) is not None:
                        raise ValueError('Snapshot actor hero-tree index exceeds one part')
                    kwargs[document] = header
                else:
                    kwargs[document] = []
        kwargs['payload_storage'] = {}
        using = router.db_for_write(
            SimcSkillDamageSnapshotActor, instance=kwargs.get('snapshot'),
        )
        with transaction.atomic(using=using):
            row = SimcSkillDamageSnapshotActor.objects.using(using).create(**kwargs)
            documents = {}
            for document, parts in pending.items():
                digest = hashlib.sha256()
                byte_count = 0
                part_count = 0
                for ordinal, text in enumerate(parts):
                    encoded = text.encode('utf-8')
                    digest.update(encoded)
                    byte_count += len(encoded)
                    SimcSkillDamageSnapshotPart.objects.using(using).create(
                        actor=row, document=document, ordinal=ordinal, text=text,
                    )
                    part_count += 1
                documents[document] = {
                    'parts': part_count, 'sha256': digest.hexdigest(), 'bytes': byte_count,
                }
            if documents:
                row.payload_storage = {'format': cls.FORMAT, 'documents': documents}
                row.save(using=using, update_fields=['payload_storage'])
            return row

    @staticmethod
    def _value(row_or_dict, field, default=None):
        if isinstance(row_or_dict, dict):
            return row_or_dict.get(field, default)
        return getattr(row_or_dict, field, default)

    @classmethod
    def _manifest(cls, row_or_dict):
        storage = cls._value(row_or_dict, 'payload_storage', {})
        if storage == {}:
            return {}
        if not isinstance(storage, dict) or set(storage) != {'format', 'documents'}:
            raise ValueError('Invalid snapshot actor storage manifest')
        documents = storage['documents']
        if (storage['format'] != cls.FORMAT or not isinstance(documents, dict)
                or not documents or not set(documents).issubset(cls.DOCUMENTS)):
            raise ValueError('Unsupported snapshot actor storage manifest')
        for metadata in documents.values():
            if not isinstance(metadata, dict) or set(metadata) != {'parts', 'sha256', 'bytes'}:
                raise ValueError('Invalid snapshot document metadata')
            parts, byte_count, digest = metadata['parts'], metadata['bytes'], metadata['sha256']
            if (type(parts) is not int or parts < 2
                    or type(byte_count) is not int or byte_count <= 0
                    or not isinstance(digest, str)
                    or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
                raise ValueError('Invalid snapshot document part count or digest')
            # Full non-final chunks have a fixed character count; UTF-8 uses
            # between one and four bytes per character. Reject impossible sizes.
            if not ((parts - 1) * cls.PART_CHARACTERS < byte_count
                    <= parts * cls.PART_CHARACTERS * 4):
                raise ValueError('Invalid snapshot document byte count')
        return documents

    @classmethod
    def _parts(cls, row_or_dict):
        actor_id = cls._value(row_or_dict, 'pk') or cls._value(row_or_dict, 'id')
        if actor_id is None:
            raise ValueError('Snapshot actor has no persisted identity')
        state = getattr(row_or_dict, '_state', None)
        using = getattr(state, 'db', None) or router.db_for_read(SimcSkillDamageSnapshotPart)
        return SimcSkillDamageSnapshotPart.objects.using(using).filter(actor_id=actor_id)

    @classmethod
    def is_complete(cls, row):
        """Cheap structural gate: only manifests and count/min/max, never text.

        Legacy inline rows (empty manifest) are complete. Cryptographic integrity
        of fragmented documents is checked on explicit read, not in this gate.
        """
        try:
            documents = cls._manifest(row)
            if not documents:
                return True
            aggregates = list(cls._parts(row).order_by().values('document').annotate(
                count=Count('id'), first=Min('ordinal'), last=Max('ordinal'),
            ))
        except (TypeError, ValueError):
            return False
        if {item['document'] for item in aggregates} != set(documents):
            return False
        return all(
            item['count'] == documents[item['document']]['parts']
            and item['first'] == 0
            and item['last'] == documents[item['document']]['parts'] - 1
            for item in aggregates
        )

    @classmethod
    def iterate_json_document(cls, row_or_dict, document):
        """Yield bounded JSON text; consume fully to verify final SHA/byte count.

        This is a low-level iterator, not an atomic verified streaming response:
        corruption can be reported after earlier chunks have already been yielded.
        """
        if document not in cls.DOCUMENTS:
            raise ValueError('Unsupported snapshot actor document')
        documents = cls._manifest(row_or_dict)
        metadata = documents.get(document)
        if metadata is None:
            yield from cls._iter_json_parts(cls._value(row_or_dict, document))
            return
        parts = cls._parts(row_or_dict).filter(document=document)
        bounds = parts.aggregate(count=Count('id'), first=Min('ordinal'), last=Max('ordinal'))
        expected = metadata['parts']
        if bounds != {'count': expected, 'first': 0, 'last': expected - 1}:
            raise ValueError('Snapshot document has missing or unexpected parts')
        digest = hashlib.sha256()
        byte_count = 0
        for start in range(0, expected, cls.READ_PARTS):
            stop = min(start + cls.READ_PARTS, expected)
            # SQL, not just Python consumption, is limited to four text rows.
            batch = list(parts.filter(ordinal__gte=start, ordinal__lt=stop)
                         .order_by('ordinal').values_list('ordinal', 'text')[:cls.READ_PARTS])
            if [ordinal for ordinal, _ in batch] != list(range(start, stop)):
                raise ValueError('Snapshot document part ordinals are not continuous')
            for ordinal, text in batch:
                if (not text or len(text) > cls.PART_CHARACTERS
                        or (ordinal < expected - 1 and len(text) != cls.PART_CHARACTERS)):
                    raise ValueError('Invalid snapshot document part length')
                encoded = text.encode('utf-8')
                digest.update(encoded)
                byte_count += len(encoded)
                yield text
        if byte_count != metadata['bytes'] or digest.hexdigest() != metadata['sha256']:
            raise ValueError('Snapshot document checksum or byte count mismatch')

    @classmethod
    def read_document(cls, row_or_dict, document):
        """Materialize one document, preserving the historical API value shape."""
        if document not in cls.DOCUMENTS:
            raise ValueError('Unsupported snapshot actor document')
        documents = cls._manifest(row_or_dict)
        if document not in documents:
            return cls._value(row_or_dict, document)
        return json.loads(''.join(cls.iterate_json_document(row_or_dict, document)))
