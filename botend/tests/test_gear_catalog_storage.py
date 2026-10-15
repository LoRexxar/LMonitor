"""Storage contracts exercised against SQLite and real temporary files/flock."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.http import FileResponse, StreamingHttpResponse
from django.test import RequestFactory, TestCase, override_settings

from botend.services import gear_catalog_snapshot as snapshots
from botend.services import gear_catalog_storage as storage
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class GearCatalogStorageTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix='gear-storage-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        override = override_settings(GEAR_CATALOG_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.key = snapshots.coordinate('Warrior', 'Fury', 'head')
        self.directory = snapshots._directory(self.key)
        self.url = '/?class=Warrior&spec=Fury&slot=head&snapshot=1'

    def build(self):
        snapshots.request_refresh(self.key)
        self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [self.key])
        return snapshots._verified_index(self.key)

    def legacy(self, *, current=True, age=0, corrupt=False):
        self.directory.mkdir(parents=True, exist_ok=True)
        generation = uuid.uuid4().hex
        content = {'items': [{'item_id': 17, 'variants': [], 'metadata': {
            'difficulty_rule_evidence': {'large': 'evidence' * 1000}}}], 'catalog': {}, 'source_groups': {}}
        payload = {'success': True, **content, 'total': 1, 'snapshot': {
            'state': 'ready', 'generation': generation, 'coordinate': self.key, 'generated_at': '2026-01-01T00:00:00Z'}}
        raw = json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False, separators=(',', ':')).encode()
        digest = hashlib.sha256(json.dumps(content, cls=DjangoJSONEncoder, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.directory / (generation + '.json')
        path.write_bytes(raw)
        compressed = gzip.compress(raw, mtime=0)
        zipped = self.directory / (path.name + '.gz')
        zipped.write_bytes(b'!' + compressed[1:] if corrupt else compressed)
        for file in (path, zipped):
            os.utime(file, (time.time() - age, time.time() - age))
        index = {'schema_version': 3, 'coordinate': self.key, 'generation': generation,
                 'file': path.name, 'bytes': len(raw), 'gzip_bytes': len(compressed),
                 'content_hash': digest, 'source_version': 'old-source-do-not-upgrade', 'built_at': time.time() - age}
        if current:
            snapshots._write(self.directory / 'index.json', index)
        return index

    def command(self, **options):
        out = io.StringIO()
        call_command('compact_gear_catalog_snapshots', stdout=out, stderr=io.StringIO(), **options)
        return json.loads(out.getvalue())

    def test_single_gzip_http_and_full_metadata_leave_database_unchanged(self):
        self.hero.metadata = {'difficulty_rule_evidence': {'large': 'facts' * 10000}, 'keep_original': [1, 2]}
        self.hero.save(update_fields=['metadata'])
        before = dict(self.hero.metadata)
        index = self.build()
        self.assertEqual(index['schema_version'], 4)
        self.assertEqual(index['file'], index['generation'] + '.json.gz')
        self.assertFalse((self.directory / (index['generation'] + '.json')).exists())
        raw = gzip.decompress((self.directory / index['file']).read_bytes())
        self.assertEqual(index['bytes'], len(raw))
        self.assertEqual(index['payload_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotIn(b'difficulty_rule_evidence', raw)
        self.hero.refresh_from_db()
        self.assertEqual(self.hero.metadata, before)
        etags = {}
        for encoding in ('gzip', '', 'gzip;q=0', 'gzip;q=0.0, *;q=1'):
            with self.assertNumQueries(0):
                response = snapshots.catalog_snapshot_response(RequestFactory().get(self.url, HTTP_ACCEPT_ENCODING=encoding))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Vary'], 'Accept-Encoding')
                body = b''.join(response.streaming_content)
                if encoding == 'gzip':
                    self.assertIsInstance(response, FileResponse)
                    self.assertEqual(gzip.decompress(body), raw)
                else:
                    self.assertIsInstance(response, StreamingHttpResponse)
                    self.assertNotIsInstance(response, FileResponse)
                    self.assertEqual(body, raw)
                    self.assertNotIn('Content-Encoding', response)
                etags[encoding] = response['ETag']
                response.close()
                cached = snapshots.catalog_snapshot_response(RequestFactory().get(
                    self.url, HTTP_ACCEPT_ENCODING=encoding, HTTP_IF_NONE_MATCH='W/' + etags[encoding]))
                self.assertEqual(cached.status_code, 304)
                self.assertEqual(cached['Vary'], 'Accept-Encoding')
        self.assertNotEqual(etags['gzip'], etags[''])

    def test_legacy_read_dry_run_conversion_and_resume_without_queries(self):
        old = self.legacy()
        with self.assertNumQueries(0):
            response = snapshots.catalog_snapshot_response(RequestFactory().get(self.url))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(b''.join(response.streaming_content))['snapshot']['generation'], old['generation'])
            response.close()
            self.command()
            self.assertEqual(snapshots._verified_index(self.key), old)
            result = self.command(apply=True)
            new = snapshots._verified_index(self.key)
            self.assertEqual(new['schema_version'], 4)
            self.assertEqual(new['source_version'], old['source_version'])
            self.assertFalse((self.directory / old['file']).exists())
            self.assertTrue((self.directory / (old['file'] + '.gz')).exists())
            self.assertTrue((self.directory / (old['generation'] + '.meta.json')).exists())
            self.command(apply=True)
            self.assertEqual(snapshots._verified_index(self.key), new)
        self.assertEqual(result['failed'], 0)

    def test_corrupt_legacy_gzip_and_index_mismatch_preserve_current(self):
        old = self.legacy(corrupt=True)
        before = (self.directory / 'index.json').read_bytes()
        with self.assertRaises(CommandError):
            self.command(apply=True)
        self.assertEqual((self.directory / 'index.json').read_bytes(), before)
        self.assertTrue((self.directory / old['file']).exists())
        self.assertEqual(len(list(self.directory.glob('*.json.gz'))), 1)
        old['generation'] = uuid.uuid4().hex
        snapshots._write(self.directory / 'index.json', old)
        self.assertIsNone(snapshots._index(self.key))

    def test_retention_keeps_two_noncurrent_plus_48h_and_reclaims_history_raw(self):
        older = [self.legacy(current=False, age=age) for age in (400000, 350000, 300000, 250000)]
        recent = self.legacy(current=False, age=60)
        current = self.legacy()
        self.command(apply=True)
        # old current + recent are the two newest noncurrent generations.
        for index in older:
            self.assertFalse((self.directory / index['file']).exists())
            self.assertFalse((self.directory / (index['file'] + '.gz')).exists())
        for index in (recent, current):
            self.assertFalse((self.directory / index['file']).exists())
            self.assertTrue((self.directory / (index['file'] + '.gz')).exists())
        self.assertIsNotNone(snapshots._verified_index(self.key))

    def test_discard_never_deletes_current_when_body_is_damaged(self):
        index = self.build()
        body = self.directory / index['file']
        body.write_bytes(b'truncated')
        storage.discard(self.directory, index)
        self.assertTrue(body.exists())
        self.assertTrue((self.directory / (index['generation'] + '.meta.json')).exists())

    def test_discard_preserves_candidate_when_current_index_cannot_be_read(self):
        index = self.build()
        (self.directory / 'index.json').write_text('{')
        storage.discard(self.directory, index)
        self.assertTrue((self.directory / index['file']).exists())

    def test_refresh_repairs_malformed_index_without_repeated_candidates(self):
        old = self.build()
        for raw in (b'{', b'null'):
            with self.subTest(raw=raw):
                pointer = self.directory / 'index.json'
                pointer.write_bytes(raw)
                before = {path.name for path in self.directory.glob('*.json.gz')}
                response = snapshots.catalog_snapshot_response(RequestFactory().get(self.url))
                self.assertEqual(response.status_code, 202)
                response.close()
                # Exercise the real worker's fast polling and publication path.
                (self.directory / 'attempt.json').unlink(missing_ok=True)
                self.assertEqual(snapshots.refresh_catalog_snapshots(poll=True), [self.key])
                new = snapshots._verified_index(self.key)
                self.assertIsNotNone(new)
                self.assertNotEqual(new['generation'], old['generation'])
                self.assertEqual({path.name for path in self.directory.glob('*.json.gz')},
                                 before | {new['file']})
                for _ in range(2):
                    self.assertEqual(self.build()['generation'], new['generation'])
                self.assertEqual({path.name for path in self.directory.glob('*.json.gz')},
                                 before | {new['file']})
                response = snapshots.catalog_snapshot_response(RequestFactory().get(self.url))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(b''.join(response.streaming_content))['snapshot']['generation'],
                                 new['generation'])
                response.close()

    def test_failed_repair_restores_exact_pointer_and_protects_unknown_current(self):
        old = self.build()
        pointer = self.directory / 'index.json'
        old_body = (self.directory / old['file']).read_bytes()
        valid_raw = json.dumps(old, indent=2).encode() + b'\n'
        for raw in (b'{', b'null', valid_raw):
            for after_commit in (False, True):
                with self.subTest(raw=raw, after_commit=after_commit):
                    pointer.write_bytes(raw)
                    candidate = storage.prepare(self.directory, self.key,
                                                {'items': [], 'catalog': {}}, 0, 'repair')
                    writes = []

                    def fail_write(path, document):
                        writes.append(document['generation'])
                        if after_commit:
                            snapshots._write(path, document)
                        raise OSError('injected index write failure')

                    with self.assertRaisesRegex(OSError, 'injected index write failure'):
                        storage.publish(self.directory, self.key, candidate,
                                        storage.read_index(self.directory, self.key), write_index=fail_write)
                    self.assertEqual(writes, [candidate['generation']])
                    self.assertEqual(pointer.read_bytes(), raw)
                    storage.discard(self.directory, candidate)
                    # Unknown pointers cannot authorize deletion, even on failure.
                    self.assertEqual((self.directory / candidate['file']).exists(), raw != valid_raw)
                    self.assertEqual((self.directory / old['file']).read_bytes(), old_body)

    def test_failed_publication_does_not_replace_independently_changed_pointer(self):
        old = self.build()
        candidate = storage.prepare(self.directory, self.key, {'items': [], 'catalog': {}}, 0, 'repair')
        pointer = self.directory / 'index.json'
        for raw in (b'{', b'null'):
            with self.subTest(raw=raw):
                snapshots._write(pointer, old)

                def change_pointer(path, document):
                    snapshots._write(path, document)
                    path.write_bytes(raw)
                    raise OSError('independent pointer change')

                with self.assertRaisesRegex(OSError, 'independent pointer change'):
                    storage.publish(self.directory, self.key, candidate, old, write_index=change_pointer)
                self.assertEqual(pointer.read_bytes(), raw)
                storage.discard(self.directory, candidate)
                self.assertTrue((self.directory / candidate['file']).exists())

    def test_collection_validates_whole_selected_batch_before_any_deletion(self):
        self.build()
        for age in (60, 120, 300000):
            self.legacy(current=False, age=age)
        self.legacy(current=False, age=400000, corrupt=True)
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with self.assertRaises(storage.ERRORS):
            storage.maintenance(self.directory, self.key, apply=True)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)
        self.assertIsNotNone(snapshots._verified_index(self.key))

    def test_busy_real_flock_no_index_and_unknown_paths_are_never_deleted(self):
        old = self.legacy()
        with snapshots._lock(self.root / 'worker.lock'):
            with self.assertRaises(CommandError):
                self.command(apply=True, lock_timeout=0)
        self.assertEqual(snapshots._verified_index(self.key), old)
        orphan = self.root / 'Warrior-Fury-chest'
        orphan.mkdir()
        evidence = orphan / ('a' * 32 + '.json')
        evidence.write_text('do not delete')
        (self.directory / 'evidence-source.json').write_text('unknown source')
        with self.assertRaises(CommandError):
            self.command(apply=True)
        self.assertTrue(evidence.exists())
        self.assertTrue((self.directory / old['file']).exists())
        (self.directory / 'evidence-source.json').unlink()
        (self.directory / ('f' * 32 + '.json.gz')).symlink_to(evidence)
        with self.assertRaises(CommandError):
            self.command(apply=True)
        self.assertEqual(evidence.read_text(), 'do not delete')
