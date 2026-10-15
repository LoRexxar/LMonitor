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
        self.assertEqual({path.name for path in output.iterdir()}, filenames | {'manifest.json'})
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
                    'manifest.json'}
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


class ActivationConstructorTests(TestCase):
    def test_facts_uses_real_keyword_only_collector(self):
        source = SimpleNamespace(_get=lambda *a, **kw: self.fail('unexpected network'))
        self.assertEqual(UpdateSource.facts(source, {'items': []}, 'retail', BUILD, lambda *_: None), ([], []))
