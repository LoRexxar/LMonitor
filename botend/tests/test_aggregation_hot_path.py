"""Real cache bytes and ORM aggregation: hints may save work, never prove a hit."""
import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import SeasonMeta, SpecAggregationWorkCache, SpecRaidRanking
from botend.services import raid_aggregation as raid
from botend.services import spec_aggregation_cache as cache
from botend.services.spec_stats_snapshot import content_digest
from botend.services.stats_storage_codec import pack_detail


class ReadMeter:
    """Observe actual decompression; optional atomic replacement at the I/O boundary."""
    def __init__(self, after_first_read=None):
        self.reads = []
        self.opens = 0
        self.after_first_read = after_first_read
        self.opener = gzip.open

    def open(self, *args, **kwargs):
        stream = self.opener(*args, **kwargs)
        self.opens += 1
        meter = self

        class Reader:
            def __enter__(self):
                stream.__enter__()
                return self

            def __exit__(self, *exc):
                return stream.__exit__(*exc)

            def read(self, size=-1):
                result = stream.read(size)
                meter.reads.append((size, len(result)))
                callback, meter.after_first_read = meter.after_first_read, None
                if callback:
                    callback()
                return result
        return Reader()


class WorkHeaderTests(SimpleTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(self.settings(SPEC_AGGREGATION_WORK_ROOT=Path(tmp.name) / 'work',
                                       SPEC_AGGREGATION_IMPORT_LEGACY_DATABASE=False))
        self.scope = 'raid:Mage:Fire:5:1'
        self.payload = {'value': '原始内容' * 100000, 'sample_size': 3}
        cache.save_work(1, self.scope, 'old', self.payload, strict=True)
        self.path = cache.work_path(1, self.scope)

    def raw(self):
        return gzip.decompress(self.path.read_bytes())

    def write_container(self, data, *, old=False, plain=False, digest=None):
        body = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
        digest = digest or hashlib.sha256(body).hexdigest().encode()
        raw = (cache.PREFIX + digest + cache.SEPARATOR + body + b'}' if old else
               cache.NEW_PREFIX + body + cache.NEW_SUFFIX + digest + b'"}')
        if plain:
            self.path.unlink(missing_ok=True)
            self.path.with_suffix('').write_bytes(raw)
        else:
            self.path.write_bytes(gzip.compress(raw))
        return raw

    def data(self):
        return json.loads(self.raw())['data']

    def test_mismatch_reads_only_bounded_header_but_match_validates_all_bytes(self):
        before = self.path.read_bytes()
        meter = ReadMeter()
        with patch.object(cache.gzip, 'open', meter.open):
            self.assertIsNone(cache.load_work(1, self.scope, 'new'))
        self.assertLessEqual(sum(size for _, size in meter.reads), 4096)
        self.assertEqual(meter.opens, 1)
        match = ReadMeter()
        with patch.object(cache.gzip, 'open', match.open):
            actual = cache.load_work(1, self.scope, 'old')
        self.assertEqual(actual, self.payload)
        self.assertEqual(content_digest(actual), content_digest(self.payload))
        self.assertEqual(sum(size for _, size in match.reads), len(self.raw()))
        self.assertEqual(match.opens, 1)
        self.assertEqual(self.path.read_bytes(), before)
        print('WORK_HEADER_BYTES', json.dumps({'miss': sum(n for _, n in meter.reads),
              'hit': sum(n for _, n in match.reads), 'compressed': len(before)}))

    def test_legacy_containers_and_unrecognized_header_keep_full_validation(self):
        data = self.data()
        for old, plain in ((True, False), (True, True), (False, False)):
            with self.subTest(old=old, plain=plain):
                # Legacy writers may use arbitrary key order: do not guess a header.
                reordered = {'payload': data['payload'], **{k: v for k, v in data.items() if k != 'payload'}}
                self.write_container(reordered, old=old, plain=plain)
                self.assertEqual(cache.load_work(1, self.scope, 'old'), self.payload)
                self.assertIsNone(cache.load_work(1, self.scope, 'new'))
                self.path.with_suffix('').unlink(missing_ok=True)
        self.write_container(data, old=True)
        meter = ReadMeter()
        with patch.object(cache.gzip, 'open', meter.open):
            self.assertIsNone(cache.load_work(1, self.scope, 'new'))
        self.assertLessEqual(sum(n for _, n in meter.reads), 4096)
        self.assertEqual(cache.load_work(1, self.scope, 'old'), self.payload)

    def test_matching_hint_is_not_authentication(self):
        original = self.data()
        for name, change in (
            ('scope', {'scope': 'other'}), ('season', {'season_id': 2}),
            ('schema', {'schema': 99}), ('fingerprint', {'fingerprint': 'new'}),
            ('codec', {'payload': {'_stats_storage': 999}}),
        ):
            with self.subTest(name=name):
                self.write_container({**original, **change})
                self.assertIsNone(cache.load_work(1, self.scope, 'old'))
        self.write_container(original, digest=b'0' * 64)
        self.assertEqual(cache.work_fingerprint_hint(1, self.scope), 'old')
        self.assertIsNone(cache.load_work(1, self.scope, 'old'))
        # A duplicate key after payload can override the thin prefix; final validation wins.
        for suffix in (b',"scope":"other"}', b',"fingerprint":"new"}'):
            raw = self.write_container(original)
            body = raw[len(cache.NEW_PREFIX):-(len(cache.NEW_SUFFIX) + 66)]
            body = body[:-1] + suffix
            self.path.write_bytes(gzip.compress(cache.NEW_PREFIX + body + cache.NEW_SUFFIX
                + hashlib.sha256(body).hexdigest().encode() + b'"}'))
            self.assertEqual(cache.work_fingerprint_hint(1, self.scope), 'old')
            self.assertIsNone(cache.load_work(1, self.scope, 'old'))

    def test_truncation_gzip_corruption_and_read_budget_never_hit(self):
        original = self.path.read_bytes()
        for broken in (original[:12], original[:-8], original[:-8] + b'0' * 8,
                       gzip.compress(self.raw()[:-20])):
            with self.subTest(size=len(broken)):
                self.path.write_bytes(broken)
                self.assertIsNone(cache.load_work(1, self.scope, 'old'))
            self.path.write_bytes(original)
        with self.settings(SPEC_AGGREGATION_WORK_MAX_BYTES=1024):
            meter = ReadMeter()
            with patch.object(cache.gzip, 'open', meter.open):
                self.assertIsNone(cache.load_work(1, self.scope, 'old'))
            self.assertLessEqual(sum(n for _, n in meter.reads), 1025)
        # Compressed size fits, decompressed size does not.
        cache.save_work(1, self.scope, 'old', {'value': 'x' * 10000}, strict=True)
        with self.settings(SPEC_AGGREGATION_WORK_MAX_BYTES=1024):
            meter = ReadMeter()
            with patch.object(cache.gzip, 'open', meter.open):
                self.assertIsNone(cache.load_work(1, self.scope, 'old'))
            self.assertLessEqual(sum(n for _, n in meter.reads), 1025)

    def test_long_header_falls_back_and_hint_is_bounded(self):
        fingerprint = 'f' * 6000
        cache.save_work(1, self.scope, fingerprint, self.payload, strict=True)
        meter = ReadMeter()
        with patch.object(cache.gzip, 'open', meter.open):
            self.assertIsNone(cache.work_fingerprint_hint(1, self.scope))
        self.assertLessEqual(sum(n for _, n in meter.reads), 4096)
        self.assertEqual(cache.load_work(1, self.scope, fingerprint), self.payload)

    def test_escaped_unicode_identity_and_plain_legacy_hint(self):
        scope, fingerprint = 'raid:法师:"火焰"\\5:1', 'v:"版本"\\1'
        cache.save_work(1, scope, fingerprint, {'value': 1}, strict=True)
        self.assertEqual(cache.work_fingerprint_hint(1, scope), fingerprint)
        self.assertEqual(cache.load_work(1, scope, fingerprint), {'value': 1})
        self.assertIsNone(cache.load_work(1, scope, 'other'))
        original = self.data()
        self.write_container(original, old=True, plain=True)
        self.assertEqual(cache.work_fingerprint_hint(1, self.scope), 'old')
        self.assertEqual(cache.load_work(1, self.scope, 'old'), self.payload)

    def test_atomic_replacement_cannot_mix_header_and_body(self):
        replacement = {'value': 'replacement'}
        meter = ReadMeter(lambda: cache.save_work(1, self.scope, 'new', replacement, strict=True))
        with patch.object(cache.gzip, 'open', meter.open):
            # Already opened inode is a valid old snapshot, not a mixed new hit.
            self.assertEqual(cache.load_work(1, self.scope, 'old'), self.payload)
        self.assertEqual(meter.opens, 1)
        self.assertIsNone(cache.load_work(1, self.scope, 'old'))
        self.assertEqual(cache.load_work(1, self.scope, 'new'), replacement)
        self.assertEqual(cache.work_fingerprint_hint(1, self.scope), 'new')
        cache.save_work(1, self.scope, 'third', {'value': 'third'}, strict=True)
        self.assertIsNone(cache.load_work(1, self.scope, 'new'))

    def test_save_contract_is_unchanged(self):
        data = {'schema': cache.SCHEMA, 'season_id': 1, 'scope': self.scope,
                'fingerprint': 'old', 'payload': pack_detail(self.payload)}
        body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        expected = cache.NEW_PREFIX + body + cache.NEW_SUFFIX + hashlib.sha256(body).hexdigest().encode() + b'"}'
        self.assertEqual(self.raw(), expected)
        before = self.path.read_bytes()
        cache.save_work(1, self.scope, 'old', self.payload, strict=True)
        self.assertEqual(self.path.read_bytes(), before)


class RaidBufferHotPathTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(self.settings(SPEC_AGGREGATION_WORK_ROOT=Path(tmp.name) / 'work',
                                       SPEC_AGGREGATION_IMPORT_LEGACY_DATABASE=False))
        self.season = SeasonMeta.objects.create(season_key='hot-path', season_name='Test',
            is_active=True, mplus_zone_id=1, raid_zone_id=2)
        self.scopes = [(self.season.pk, boss, f'Boss{boss}', 'Mage', 'Fire', 5) for boss in (1, 2, 3)]
        self.rows = [SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=boss,
            boss_name=name, difficulty=5, class_name='Mage', spec_name='Fire',
            character_name='Player', realm='Realm', region='eu', dps=100,
            report_code='Report', fight_id=boss) for _, boss, name, *_ in self.scopes]

    def one_scope_budget(self):
        buffer = raid.RaidInputBuffer()
        scope = self.scopes[-1]
        rows, facts = raid._inputs(scope[0], scope[1], scope[3], scope[4], scope[5], None, None)
        fingerprint = raid._fingerprint(rows, facts, scope[2], scope[3], scope[4])
        buffer.remember(scope, None, None, rows, facts, fingerprint)
        return buffer.used_bytes + 256

    @staticmethod
    def input_queries(queries):
        return sum(f'FROM "{SpecRaidRanking._meta.db_table}"' in q['sql'].replace('`', '"')
                   for q in queries)

    def test_later_changed_scope_is_not_evicted_by_earlier_hits(self):
        previous = [raid.raid_stats(*scope) for scope in self.scopes]
        SpecRaidRanking.objects.filter(pk=self.rows[-1].pk).update(dps=333)
        budget = self.one_scope_budget()
        buffer = raid.RaidInputBuffer(max_bytes=budget)
        expected_fingerprints = [raid.raid_fingerprint(*scope) for scope in self.scopes]
        fingerprints = raid.raid_fingerprints(self.scopes, prepared_inputs=buffer)
        self.assertEqual(fingerprints, expected_fingerprints)
        self.assertGreater(buffer.used_bytes, 0)
        self.assertLessEqual(buffer.used_bytes, budget)
        with CaptureQueriesContext(connection) as queries:
            actual = [raid.raid_stats(*scope, fingerprint=fingerprint, prepared_inputs=buffer)
                      for scope, fingerprint in zip(self.scopes, fingerprints)]
        self.assertEqual(self.input_queries(queries), 0)
        self.assertEqual([row[2] for row in actual], [True, True, False])
        self.assertEqual(actual[:2], [(value, fp, True) for value, fp, _ in previous[:2]])
        self.assertEqual(actual[-1][0]['dps']['avg'], 333)
        self.assertEqual(buffer.used_bytes, 0)
        # Independent computation, not a cache replay of the result under test.
        last = self.scopes[-1]
        cache.work_path(last[0], f'raid:{last[3]}:{last[4]}:{last[5]}:{last[1]}').unlink()
        oracle = raid.raid_stats(*self.scopes[-1])
        self.assertFalse(oracle[2])
        self.assertEqual((content_digest(actual[-1][0]), actual[-1][1]),
                         (content_digest(oracle[0]), oracle[1]))
        print('RAID_HOT_BUFFER', json.dumps({'budget': budget, 'scope_count': len(self.scopes),
              'compute_input_reads': self.input_queries(queries), 'hits': [r[2] for r in actual]}))

    def test_all_miss_overflow_rereads_and_recomputes_fingerprint_safely(self):
        buffer = raid.RaidInputBuffer(max_bytes=self.one_scope_budget())
        fingerprints = raid.raid_fingerprints(self.scopes, prepared_inputs=buffer)
        self.assertLessEqual(buffer.used_bytes, buffer.max_bytes)
        SpecRaidRanking.objects.filter(pk=self.rows[-1].pk).update(dps=456)
        with CaptureQueriesContext(connection) as queries:
            actual = [raid.raid_stats(*scope, fingerprint=fingerprint, prepared_inputs=buffer)
                      for scope, fingerprint in zip(self.scopes, fingerprints)]
        self.assertEqual(self.input_queries(queries), 2)
        self.assertEqual([row[2] for row in actual], [False] * 3)
        self.assertEqual(actual[-1][0]['dps']['avg'], 456)
        self.assertNotEqual(actual[-1][1], fingerprints[-1])
        self.assertEqual(buffer.used_bytes, 0)
        print('RAID_ALL_MISS_BUFFER', json.dumps({'compute_input_reads': self.input_queries(queries)}))

    def test_forged_matching_hint_only_loses_reuse_never_returns_corrupt_payload(self):
        fingerprints = raid.raid_fingerprints(self.scopes)
        for scope, fingerprint in zip(self.scopes, fingerprints):
            cache.save_work(scope[0], f'raid:{scope[3]}:{scope[4]}:{scope[5]}:{scope[1]}',
                            fingerprint, {'forged': True}, strict=True)
            path = cache.work_path(scope[0], f'raid:{scope[3]}:{scope[4]}:{scope[5]}:{scope[1]}')
            raw = gzip.decompress(path.read_bytes())
            path.write_bytes(gzip.compress(raw[:-66] + b'0' * 64 + b'"}'))
        buffer = raid.RaidInputBuffer()
        self.assertEqual(raid.raid_fingerprints(self.scopes, prepared_inputs=buffer), fingerprints)
        self.assertEqual(buffer.used_bytes, 0)
        with CaptureQueriesContext(connection) as queries:
            result, fingerprint, hit = raid.raid_stats(*self.scopes[-1],
                fingerprint=fingerprints[-1], prepared_inputs=buffer)
        self.assertEqual(self.input_queries(queries), 1)
        self.assertFalse(hit)
        self.assertNotIn('forged', result)
        self.assertEqual(result['dps']['avg'], 100)
        self.assertEqual(fingerprint, fingerprints[-1])

    def test_explicit_database_import_keeps_corrupt_file_fallback(self):
        scope = 'legacy-import'
        cache.save_work(self.season.pk, scope, 'different', {'stale': True}, strict=True)
        path = cache.work_path(self.season.pk, scope)
        raw = gzip.decompress(path.read_bytes())
        path.write_bytes(gzip.compress(raw[:-66] + b'0' * 64 + b'"}'))
        payload = {'imported': True}
        SpecAggregationWorkCache.objects.create(season=self.season, scope=scope,
            fingerprint='wanted', payload=payload, payload_hash=content_digest(payload))
        with self.settings(SPEC_AGGREGATION_IMPORT_LEGACY_DATABASE=True):
            self.assertEqual(cache.load_work(self.season.pk, scope, 'wanted'), payload)
        self.assertEqual(cache.load_work(self.season.pk, scope, 'wanted'), payload)
