"""Real loopback HTTP exercises downloader/checkpoints; no upstream network."""
import csv
import html
import io
import json
import os
import tempfile
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, TestCase
from botend.services.journal_source import WagoJournalSource
from botend.services.wow_data_update import TALENT_TABLES, UpdateSource

BUILD = '12.1.0.101'


class DownloadLifecycleTests(SimpleTestCase):
    def test_deep_old_completion_blocks_maintenance_not_download(self):
        from botend.services.download_retention import maintain_download_parts
        self.total = 2
        source = self.source()
        source.table('SpellEffect')
        source.table('SpellEffect')
        manifest_path = next(source.directory.glob('*.parts/*/manifest.json'))
        manifest = json.loads(manifest_path.read_bytes())
        old = next(p for p in source.directory.rglob('complete.json')
                   if p.parent.name != manifest['attempt'])
        old.write_text('[' * 1100 + '0' + ']' * 1100)
        saved = {p: p.read_bytes() for p in old.parent.iterdir() if p.is_file()}
        csv_path = source.directory / 'SpellEffect.csv'
        previous_csv = csv_path.read_bytes()
        report = maintain_download_parts(manifest_path.parent.parent, apply=True)
        self.assertTrue(report['blocked'])
        for key in ('deleted_files', 'deleted_bytes', 'deleted_attempts'):
            self.assertEqual(report[key], 0)
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)
        self.assertEqual(csv_path.read_bytes(), previous_csv)
        before = sum(self.calls.values())
        with self.assertLogs('botend.services.journal_source', level='WARNING') as logs:
            rows = source.table('SpellEffect')
        self.assertGreater(sum(self.calls.values()), before)
        self.assertEqual([str(row['ID']) for row in rows], ['1', '2'])
        self.assertEqual([row['Name'] for row in rows],
                         [f'{BUILD}-zhCN-1', f'{BUILD}-zhCN-2'])
        with csv_path.open(newline='') as stream:
            published = list(csv.DictReader(stream))
        self.assertEqual(published, [{'ID': '1', 'Name': f'{BUILD}-zhCN-1'},
                                     {'ID': '2', 'Name': f'{BUILD}-zhCN-2'}])
        current = json.loads(manifest_path.read_bytes())
        self.assertNotEqual(current['attempt'], manifest['attempt'])
        proof = json.loads((manifest_path.parent / current['attempt'] / 'complete.json').read_bytes())
        self.assertEqual(proof['manifest']['attempt'], current['attempt'])
        self.assertIn('recursion', str(logs.output).lower())
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)

    def test_retention_checkpoint_pre_and_post_faults_do_not_fail_table(self):
        from botend.services.download_retention import _Budget
        self.total = 2
        original = _Budget.save_checkpoint
        for phase in (1, 2):
            source = self.source(build=f'12.1.0.{phase}')
            calls = []
            def fail(budget, root):
                calls.append(root)
                if len(calls) == phase:
                    raise OSError('injected checkpoint save')
                return original(budget, root)
            with patch.object(_Budget, 'save_checkpoint', fail):
                with self.assertLogs('botend.services.journal_source', level='WARNING') as logs:
                    source.table('SpellEffect')
            self.assertTrue((source.directory / 'SpellEffect.csv').exists())
            self.assertTrue(list(source.directory.rglob('complete.json')))
            self.assertIn('checkpoint', str(logs.output))
        self.assertGreater(sum(self.calls.values()), 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = Counter()
        self.fail_offset = None
        self.fail_table = None
        self.total = 5001
        self.per_page = 25
        self.later_per_page = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                build = query['build'][0]
                locale = query['locale'][0]
                table = url.path.split('/')[3] if '/api/' in url.path else url.path.split('/')[2]
                if url.path.endswith('/csv'):
                    offset = int(query.get('filter[ID]', ['>0'])[0][1:])
                    owner.calls[build, table, locale, offset] += 1
                    if offset == owner.fail_offset or table == owner.fail_table:
                        self.send_error(503)
                        return
                    buffer = io.StringIO()
                    writer = csv.writer(buffer)
                    writer.writerow(['ID', 'Name'])
                    writer.writerows((i, f'{build}-{locale}-{i}') for i in range(offset + 1, owner.total + 1))
                    content = buffer.getvalue().encode()
                elif '/api/' in url.path:
                    per_page = owner.later_per_page if 'page' in query and owner.later_per_page else owner.per_page
                    offset = (int(query.get('page', ['1'])[0]) - 1) * per_page
                    content = json.dumps({'total': owner.total, 'per_page': per_page,
                                          'data': [{'ID': offset + 1}]}).encode()
                else:
                    props = {'currentVersion': build, 'data': {'total': owner.total}}
                    content = ('<div data-page="' + html.escape(json.dumps({'props': props}), quote=True) + '"></div>').encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        base = f'http://127.0.0.1:{self.server.server_port}'

        class Session(requests.Session):
            def request(self, method, url, **kwargs):
                return super().request(method, url.replace('https://wago.tools', base), **kwargs)

        transport = patch('botend.services.journal_source.http_session', Session)
        transport.start()
        self.addCleanup(transport.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def source(self, build=BUILD, refresh=True, branch='retail'):
        source = WagoJournalSource(build, self.root, refresh=refresh, max_workers=1)
        source.directory = self.root / 'shared'
        source.branch = branch
        return source

    def test_same_build_retry_reuses_only_verified_completed_parts(self):
        self.fail_offset = 5000
        with self.assertRaises(requests.HTTPError):
            self.source().table('SpellEffect')
        self.fail_offset = None
        self.assertEqual(len(self.source().table('SpellEffect')), 5001)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 1)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 5000], 2)

    def test_cross_build_same_total_does_not_reuse_parts_or_csv(self):
        self.source().table('SpellEffect')
        other = '12.1.0.102'
        rows = self.source(other, refresh=False).table('SpellEffect')
        self.assertEqual(rows[0]['Name'], f'{other}-zhCN-1')
        self.assertEqual(self.calls[other, 'SpellEffect', 'zhCN', 0], 1)

    def test_branch_and_locale_are_part_of_identity(self):
        self.source().segmented_table('SpellEffect', 'zhCN')
        self.source(refresh=False, branch='ptr').segmented_table('SpellEffect', 'zhCN')
        self.source(refresh=False).segmented_table('SpellEffect', 'enUS')
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 2)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'enUS', 0], 1)

    def test_corrupt_part_is_redownloaded_and_unknown_legacy_preserved(self):
        self.source(refresh=False).segmented_table('SpellEffect', 'zhCN')
        parts = list((self.root / 'shared').rglob('0-*.json'))
        self.assertEqual(len(parts), 1)
        parts[0].write_text('{broken')
        legacy = self.root / 'shared' / 'SpellEffect.zhCN.parts' / 'unknown.json'
        legacy.write_text('legacy evidence')
        self.source(refresh=False).segmented_table('SpellEffect', 'zhCN')
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 2)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 5000], 1)
        self.assertEqual(legacy.read_text(), 'legacy evidence')

    def test_mid_download_failure_keeps_old_csv(self):
        source = self.source()
        source.directory.mkdir(parents=True)
        old = source.directory / 'SpellEffect.csv'
        old.write_bytes(b'ID,Name\n99,last-good\n')
        self.fail_offset = 5000
        with self.assertRaises(requests.HTTPError):
            source.table('SpellEffect')
        self.assertEqual(old.read_bytes(), b'ID,Name\n99,last-good\n')
        self.assertNotIn('SpellEffect', source.manifest)

    def test_csv_metadata_replace_failure_preserves_the_old_pair(self):
        self.total = 2
        source = self.source()
        source.table('SpellEffect')
        path = source.directory / 'SpellEffect.csv'
        metadata = path.with_suffix('.csv.manifest.json')
        previous = (path.read_bytes(), metadata.read_bytes())
        self.total = 3
        real_replace = os.replace

        def fail_metadata(src, dst):
            if Path(dst) == metadata:
                raise OSError('metadata destination unavailable')
            return real_replace(src, dst)

        with patch('os.replace', fail_metadata):
            with self.assertRaisesRegex(OSError, 'metadata destination'):
                self.source().table('SpellEffect')
        self.assertEqual((path.read_bytes(), metadata.read_bytes()), previous)
        proofs = list((source.directory / 'SpellEffect.zhCN.parts').rglob('complete.json'))
        self.assertEqual(len(proofs), 1)
        self.assertEqual(len(self.source().table('SpellEffect')), 3)

    def test_first_csv_metadata_failure_does_not_leave_half_a_pair(self):
        self.total = 2
        source = self.source()
        path = source.directory / 'SpellEffect.csv'
        metadata = path.with_suffix('.csv.manifest.json')
        real_replace = os.replace

        def fail_metadata(src, dst):
            if Path(dst) == metadata:
                raise OSError('metadata destination unavailable')
            return real_replace(src, dst)

        with patch('os.replace', fail_metadata):
            with self.assertRaisesRegex(OSError, 'metadata destination'):
                source.table('SpellEffect')
        self.assertFalse(path.exists())
        self.assertFalse(metadata.exists())
        self.assertEqual(list(source.directory.rglob('complete.json')), [])

    def test_talent_failure_preserves_current_and_marker(self):
        self.total = 2
        output = self.root / 'talents' / 'current'
        output.mkdir(parents=True)
        old = output / 'TraitDefinition.csv'
        old.write_bytes(b'old current')
        marker = self.root / 'source_build.txt'
        marker.write_text('12.1.0.100')
        self.fail_table = 'TraitEdge'
        with patch('botend.services.wow_data_update.TALENT_TABLES', ('TraitDefinition', 'TraitEdge')):
            with self.assertRaises(requests.HTTPError):
                UpdateSource.talents(self.root, BUILD, lambda *_: None)
        self.assertEqual(old.read_bytes(), b'old current')
        self.assertEqual(marker.read_text(), '12.1.0.100')
        self.assertFalse((output / 'manifest.json').exists())

    def test_talents_fetch_zhcn_once_and_write_both_aliases(self):
        self.total = 2
        output = UpdateSource.talents(self.root, BUILD, lambda *_: None, branch='retail')
        expected = {(table, 'zhCN') for table in TALENT_TABLES}
        expected.update((table, locale) for table in ('SpellName', 'Spell', 'TraitDefinition')
                        for locale in ('enUS', 'zhCN'))
        self.assertEqual(set(self.calls), {(BUILD, table, locale, 0) for table, locale in expected})
        self.assertTrue(all(count == 1 for count in self.calls.values()))
        for table in set(TALENT_TABLES) & {'SpellName', 'Spell', 'TraitDefinition'}:
            self.assertEqual((output / f'{table}.csv').read_bytes(),
                             (output / f'{table}_zhCN.csv').read_bytes())
        filenames = {f'{table}.csv' for table in TALENT_TABLES}
        filenames.update(f'{table}_{locale}.csv' for table in ('SpellName', 'Spell', 'TraitDefinition')
                         for locale in ('enUS', 'zhCN'))
        self.assertEqual({path.name for path in output.iterdir()}, filenames | {'manifest.json', 'retention_owner.json'})
        self.assertEqual(json.loads((output / 'retention_owner.json').read_text()),
                         {'branch': 'retail', 'purpose': 'talent-source-staging'})
        manifest = json.loads((output / 'manifest.json').read_bytes())
        self.assertEqual(len(manifest), len(expected))
        self.assertEqual((self.root / 'source_build.txt').read_text(), BUILD)

    def test_talent_retry_reuses_completed_tables_without_completion_marker(self):
        self.total = 2
        self.fail_table = 'Spell'
        with patch('botend.services.wow_data_update.TALENT_TABLES', ('TraitDefinition',)):
            with self.assertRaises(requests.HTTPError):
                UpdateSource.talents(self.root, BUILD, lambda *_: None)
            self.assertFalse((self.root / 'source_build.txt').exists())
            self.fail_table = None
            first = UpdateSource.talents(self.root, BUILD, lambda *_: None)
            second = UpdateSource.talents(self.root, BUILD, lambda *_: None)
        self.assertEqual(first, second)
        self.assertEqual(self.calls[BUILD, 'TraitDefinition', 'zhCN', 0], 1)

    def test_valid_json_with_bad_hash_redownloads_only_damaged_segment(self):
        self.source(refresh=False).segmented_table('SpellEffect', 'zhCN')
        part = next((self.root / 'shared').rglob('0-*.json'))
        saved = json.loads(part.read_bytes())
        saved['rows'][0]['Name'] = 'tampered'
        part.write_text(json.dumps(saved))
        self.source(refresh=False).segmented_table('SpellEffect', 'zhCN')
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 2)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 5000], 1)

    def test_atomic_replace_failure_keeps_old_csv_and_incomplete_manifest(self):
        source = self.source()
        source.directory.mkdir(parents=True)
        old = source.directory / 'SpellEffect.csv'
        old.write_bytes(b'old current')
        replace = os.replace

        def fail_csv(src, dst):
            if Path(dst) == old:
                raise OSError('injected disk error')
            return replace(src, dst)

        with patch('botend.services.download_checkpoint.os.replace', side_effect=fail_csv):
            with self.assertRaisesRegex(OSError, 'disk error'):
                source.table('SpellEffect')
        self.assertEqual(old.read_bytes(), b'old current')
        manifest = next(source.directory.glob('*.parts/*/manifest.json'))
        self.assertEqual(json.loads(manifest.read_bytes())['state'], 'downloading')
        self.assertFalse(list(source.directory.rglob('*.writing-*')))
        self.source().table('SpellEffect')
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 1)
        self.assertEqual(json.loads(manifest.read_bytes())['state'], 'complete')

    def test_pagination_layout_change_does_not_reuse_parts(self):
        self.source(refresh=False).segmented_table('SpellEffect', 'zhCN')
        self.per_page = 50
        self.source(refresh=False).segmented_table('SpellEffect', 'zhCN')
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 2)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 5000], 2)

    def test_pagination_layout_change_during_download_is_rejected(self):
        self.later_per_page = 10
        with self.assertRaisesRegex(ValueError, '分页'):
            self.source().table('SpellEffect')
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 2000], 0)
        self.assertFalse((self.root / 'shared' / 'SpellEffect.csv').exists())

    def test_talent_candidate_rename_failure_preserves_current_and_marker(self):
        self.total = 2
        current = self.root / 'talents' / 'current'
        current.mkdir(parents=True)
        old = current / 'TraitDefinition.csv'
        old.write_bytes(b'old current')
        marker = self.root / 'source_build.txt'
        marker.write_text('12.1.0.100')
        with patch('botend.services.wow_data_update.TALENT_TABLES', ('TraitDefinition',)), \
                patch('botend.services.wow_data_update.os.rename', side_effect=OSError('injected rename')):
            with self.assertRaisesRegex(OSError, 'injected rename'):
                UpdateSource.talents(self.root, BUILD, lambda *_: None, branch='retail')
        self.assertEqual(old.read_bytes(), b'old current')
        self.assertEqual(marker.read_text(), '12.1.0.100')
        self.assertEqual(list(current.parent.iterdir()), [current])
        with patch('botend.services.wow_data_update.TALENT_TABLES', ('TraitDefinition',)):
            output = UpdateSource.talents(self.root, BUILD, lambda *_: None, branch='retail')
        self.assertNotEqual(output, current)
        expected = {'TraitDefinition.csv', 'TraitDefinition_zhCN.csv', 'TraitDefinition_enUS.csv',
                    'SpellName_enUS.csv', 'SpellName_zhCN.csv', 'Spell_enUS.csv', 'Spell_zhCN.csv',
                    'manifest.json', 'retention_owner.json'}
        self.assertEqual({path.name for path in output.iterdir()}, expected)
        manifest = json.loads((output / 'manifest.json').read_bytes())
        self.assertEqual(len(manifest), 6)
        self.assertTrue(all(entry['identity']['branch'] == 'retail' for entry in manifest.values()))
        self.assertEqual(self.calls[BUILD, 'TraitDefinition', 'zhCN', 0], 1)
        self.assertEqual(old.read_bytes(), b'old current')

    def test_explicit_refresh_after_completed_table_fetches_again(self):
        self.source().table('SpellEffect')
        self.source().table('SpellEffect')
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 2)


    def aged_attempt(self):
        import time
        self.total = 2
        self.source().table('SpellEffect')
        manifest_path = next((self.root / 'shared').glob('*.parts/*/manifest.json'))
        manifest = json.loads(manifest_path.read_bytes())
        proof = manifest_path.parent / manifest['attempt'] / 'complete.json'
        old = time.time() - 49 * 3600
        payload = json.loads(proof.read_bytes())
        payload['completed_at'] = old
        proof.write_text(json.dumps(payload))
        for path in proof.parent.iterdir():
            os.utime(path, (old, old))
        return proof.parent

    def test_automatic_refresh_reclaims_only_aged_proven_attempt(self):
        old = self.aged_attempt()
        csv_path = self.root / 'shared' / 'SpellEffect.csv'
        expected = csv_path.read_bytes()
        for _ in range(3):
            self.source().table('SpellEffect')
        self.assertFalse(old.exists())
        self.assertEqual(csv_path.read_bytes(), expected)
        self.assertEqual(len(list((self.root / 'shared').rglob('complete.json'))), 3)

    def test_repeated_aged_refreshes_keep_one_attempt(self):
        for _ in range(5):
            old = self.aged_attempt()
            self.source().table('SpellEffect')
            self.assertFalse(old.exists())
            # aged_attempt itself refreshed; the preceding current remains in grace.
            proofs = list((self.root / 'shared').rglob('complete.json'))
            self.assertTrue(proofs)
            import time
            aged = time.time() - 49 * 3600
            for proof in proofs:
                value = json.loads(proof.read_bytes())
                value['completed_at'] = aged
                proof.write_text(json.dumps(value))
                for path in proof.parent.iterdir():
                    os.utime(path, (aged, aged))
            self.source().table('SpellEffect')
            self.assertEqual(len(list((self.root / 'shared').rglob('complete.json'))), 1)

    def test_retention_dryrun_current_grace_and_legacy(self):
        from botend.services.download_retention import maintain_download_parts
        old = self.aged_attempt()
        root = old.parent.parent
        self.assertEqual(maintain_download_parts(root, apply=True)['deleted_attempts'], 0)
        with patch('botend.services.download_retention.maintain_download_parts', return_value={}):
            self.source().table('SpellEffect')
        legacy = root / '0-2.json'
        legacy.write_text('unknown')
        result = maintain_download_parts(root)
        self.assertIn('eligible', {item['status'] for item in result['entries']})
        self.assertTrue(old.exists())
        result = maintain_download_parts(root, apply=True)
        self.assertEqual(result['deleted_attempts'], 1)
        self.assertFalse(old.exists())
        self.assertEqual(legacy.read_text(), 'unknown')
        self.assertEqual(len(list(root.rglob('complete.json'))), 1)

    def test_unknown_proof_shape_corruption_links_and_missing_proof_preserved(self):
        from botend.services.download_retention import maintain_download_parts
        for damage in ('missing', 'shape', 'hash', 'symlink', 'hardlink', 'identity', 'extra'):
            with self.subTest(damage=damage):
                old = self.aged_attempt()
                root = old.parent.parent
                with patch('botend.services.download_retention.maintain_download_parts', return_value={}):
                    self.source().table('SpellEffect')
                proof = old / 'complete.json'
                part = old / '0-2.json'
                if damage == 'missing':
                    proof.unlink()
                elif damage in ('shape', 'identity'):
                    data = json.loads(proof.read_bytes())
                    if damage == 'shape':
                        data['unknown_reference'] = 'elsewhere'
                    else:
                        data['manifest']['identity']['build'] = '0.0.0.0'
                    proof.write_text(json.dumps(data))
                elif damage == 'hash':
                    part.write_text('{}')
                elif damage in ('symlink', 'hardlink'):
                    external = self.root / f'external-{damage}'
                    part.rename(external)
                    if damage == 'symlink':
                        part.symlink_to(external)
                    else:
                        os.link(external, part)
                else:
                    (old / 'unknown.json').write_text('{}')
                import time
                aged = time.time() - 49 * 3600
                for path in old.iterdir():
                    os.utime(path, (aged, aged), follow_symlinks=False)
                result = maintain_download_parts(root, apply=True)
                item = next(item for item in result['entries'] if item['path'] == str(old))
                self.assertIn('reason', item)
                self.assertTrue(old.exists())

    def test_csv_publish_failure_has_no_completion_proof(self):
        self.total = 2
        replace = os.replace
        def fail_csv(src, dst):
            if str(dst).endswith('.csv'):
                raise OSError('publish failed')
            return replace(src, dst)
        with patch('botend.services.download_checkpoint.os.replace', side_effect=fail_csv):
            with self.assertRaises(OSError):
                self.source().table('SpellEffect')
        self.assertEqual(list((self.root / 'shared').rglob('complete.json')), [])
        self.source().table('SpellEffect')
        self.assertEqual(len(list((self.root / 'shared').rglob('complete.json'))), 1)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 1)

    def test_maintenance_waits_for_same_download_lock(self):
        from botend.services.download_retention import download_lock, maintain_download_parts
        root = self.root / 'SpellEffect.zhCN.parts'
        started = threading.Event()
        done = threading.Event()
        def worker():
            started.set()
            maintain_download_parts(root)
            done.set()
        with download_lock(root):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(done.wait(0.1))
        thread.join(2)
        self.assertTrue(done.is_set())

    def test_attempt_cap_stops_refresh_without_touching_current_csv(self):
        self.total = 2
        self.source().table('SpellEffect')
        manifest = next((self.root / 'shared').glob('*.parts/*/manifest.json'))
        before = manifest.read_bytes()
        csv_path = self.root / 'shared' / 'SpellEffect.csv'
        csv_before = csv_path.read_bytes()
        for index in range(1022):
            (manifest.parent / f'{index:032x}').mkdir()
        with self.assertRaisesRegex(ValueError, 'attempt budget exhausted'):
            self.source().table('SpellEffect')
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(csv_path.read_bytes(), csv_before)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 1)

    def test_root_budget_blocks_new_attempt_but_allows_resume(self):
        self.total = 2
        self.source().segmented_table('SpellEffect', 'zhCN')
        root = self.root / 'shared' / 'SpellEffect.zhCN.parts'
        for index in range(1024):
            (root / f'legacy-{index}').write_text('unknown')
        # Existing in-flight attempt can finish even when root is over budget.
        self.source().table('SpellEffect')
        manifest = next(root.glob('*/manifest.json'))
        before = manifest.read_bytes()
        with self.assertRaisesRegex(ValueError, 'root budget'):
            self.source().table('SpellEffect')
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(self.calls[BUILD, 'SpellEffect', 'zhCN', 0], 1)

    def test_invalid_current_manifest_blocks_reclamation(self):
        from botend.services.download_retention import maintain_download_parts
        old = self.aged_attempt()
        with patch('botend.services.download_retention.maintain_download_parts', return_value={}):
            self.source().table('SpellEffect')
        manifest = old.parent / 'manifest.json'
        saved = json.loads(manifest.read_bytes())
        saved['csv']['unknown_reference'] = str(old)
        manifest.write_text(json.dumps(saved))
        self.assertEqual(maintain_download_parts(old.parent.parent, apply=True)['deleted_attempts'], 0)
        self.assertTrue(old.exists())

    def test_command_defaults_dryrun_and_budget_fails_closed(self):
        from django.core.management import call_command
        from botend.services.download_retention import maintain_download_parts
        old = self.aged_attempt()
        root = old.parent.parent
        with patch('botend.services.download_retention.maintain_download_parts', return_value={}):
            self.source().table('SpellEffect')
        output = io.StringIO()
        call_command('maintain_download_parts', str(root), stdout=output)
        self.assertFalse(json.loads(output.getvalue())['apply'])
        self.assertTrue(old.exists())
        result = maintain_download_parts(root, apply=True, max_entries=2)
        self.assertEqual(result['deleted_attempts'], 0)
        self.assertTrue(old.exists())


class ActivationConstructorTests(TestCase):
    def test_facts_uses_real_keyword_only_collector(self):
        source = SimpleNamespace(_get=lambda *a, **kw: self.fail('unexpected network'))
        self.assertEqual(UpdateSource.facts(source, {'items': []}, 'retail', BUILD, lambda *_: None), ([], []))
