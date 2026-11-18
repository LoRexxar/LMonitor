"""Real loopback HTTP + production Inertia parser; no upstream requests."""
import copy
import html
import json
import tempfile
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor, WagoDiffUnavailable
from botend.models import WowWagoMonitorState, WowHotfixReport


class WagoPushCheckpointTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(override_settings(BASE_DIR=self.root))
        self.requests = []
        self.fail_push = None
        self.root_sample = None
        self.stable_terminal_pages = False
        self.hotfix_payload_overrides = {}
        self.http_errors = {}
        self.rows = [dict(id=i + 1, push_id=100, region_id=3, locale='enUS',
                          table_name='SpellEffect', record_id=rid, build=70575,
                          data=[rid, 'original'])
                     for i, rid in enumerate((810, 811, 820, 821))]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlsplit(self.path)
                params = parse_qs(parts.query)
                query, page = params['search'][0], int(params['page'][0])
                owner.requests.append({'path': parts.path, 'search': query, 'page': page})
                pending = owner.http_errors.get((parts.path, query))
                if pending:
                    status, retry_after = pending.pop(0)
                    self.send_response(status)
                    if retry_after is not None:
                        self.send_header('Retry-After', retry_after)
                    self.end_headers()
                    return
                tokens = query.split()
                locale, push = tokens[0], None if tokens[1] == '%' else int(tokens[1])
                prefix = tokens[2] if len(tokens) > 2 else ''
                rows = [copy.deepcopy(r) for r in owner.rows if r['locale'] == locale
                        and (r['push_id'] == push if push is not None else str(r['build']) == prefix)]
                if push is None:
                    pass  # Build-qualified candidate discovery, not a record prefix.
                elif len(tokens) > 3:
                    rows = [r for r in rows if str(r['record_id']) == prefix and str(r['build']).startswith(tokens[3])]
                else:
                    rows = [r for r in rows if str(r['record_id']).startswith(prefix)]
                total = len(rows)
                # Deliberately unstable multi-page ordering; real parser must
                # recover via conserved prefix queries, never union snapshots.
                data = rows[(page - 1) * 2:page * 2] if owner.stable_terminal_pages and len(tokens) > 3 else rows[:2]
                if push is not None and not prefix and data and owner.root_sample is not None:
                    data[0] = owner.root_sample
                if push is not None and push == owner.fail_push:
                    total, data = 1, []
                props = {'filters': {'search': query}, 'hotfixes': {
                    'data': data, 'total': total, 'current_page': page,
                    'last_page': max(1, (total + 1) // 2), 'per_page': 2}}
                if (query, page) in owner.hotfix_payload_overrides:
                    props['hotfixes'] = owner.hotfix_payload_overrides[query, page]
                payload = json.dumps({'props': props}).replace('Infinity', '1e309')
                body = ('<div id="app" data-page="' + html.escape(payload, quote=True) + '"></div>').encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        # Block every non-loopback request, including accidental downstream I/O.
        original = requests.sessions.Session.request
        def guarded(session, method, url, **kwargs):
            if urlsplit(url).hostname != '127.0.0.1':
                raise AssertionError('external HTTP forbidden')
            return original(session, method, url, **kwargs)
        self.enterContext(patch.object(requests.sessions.Session, 'request', guarded))

    def monitor(self):
        monitor = WagoSkillDiffMonitor(None, None)
        def transport(url, **kwargs):
            parts = urlsplit(url)
            self.assertEqual(parts.netloc, 'wago.tools')
            return self.session.get(f'http://127.0.0.1:{self.server.server_port}{parts.path}?{parts.query}', timeout=5)
        self.enterContext(patch.object(monitor._http_session, 'get', side_effect=transport))
        self.addCleanup(monitor._http_session.close)
        return monitor

    def collect(self, monitor=None, **kwargs):
        return (monitor or self.monitor())._collect_hotfix_interval_rows(
            kwargs.pop('from_push', 99), kwargs.pop('to_push', 100),
            region_id=kwargs.pop('region_id', 3), locale=kwargs.pop('locale', 'enUS'), **kwargs)

    def test_partition_starts_without_three_failed_pagination_sweeps(self):
        self.assertEqual(self.collect(), self.rows)
        self.assertFalse([r for r in self.requests if r['page'] != 1])
        self.assertEqual(sum(r['search'] == 'enUS 100' for r in self.requests), 1)

    def test_partition_prefers_observed_digits_under_query_budget(self):
        self.rows = [dict(row, record_id=rid) for row, rid in zip(self.rows, (998, 999, 901, 902))]
        with override_settings(WAGO_HOTFIX_MAX_PREFIX_QUERIES=3):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'prefix search limit'):
                self.collect()
        self.assertEqual(len(self.requests), 3)
        self.assertFalse((self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json').exists())
        self.requests.clear()
        with override_settings(WAGO_HOTFIX_MAX_PREFIX_QUERIES=4):
            self.assertEqual(self.collect(), self.rows)
        self.assertEqual([r['search'] for r in self.requests], [
            'enUS 100', 'enUS 100 9', 'enUS 100 99', 'enUS 100 90',
        ])

    @override_settings(WAGO_HOTFIX_MAX_PREFIX_QUERIES=8)
    def test_dense_exact_record_queries_terminal_first_without_losing_descendant(self):
        self.stable_terminal_pages = True
        self.rows = [dict(row, record_id=810, build=70575 + i) for i, row in enumerate(self.rows)]
        self.rows.append(dict(self.rows[0], id=5, record_id=8101))
        self.assertEqual(self.collect(), self.rows)
        self.assertEqual([(r['search'], r['page']) for r in self.requests], [
            ('enUS 100', 1), ('enUS 100 8', 1), ('enUS 100 81', 1), ('enUS 100 810', 1),
            ('enUS 100 810 7', 1), ('enUS 100 810 7', 2),
            ('enUS 100 8100', 1), ('enUS 100 8101', 1),
        ])

    def test_terminal_fast_path_cannot_hide_observed_descendant_at_inner_prefix(self):
        self.stable_terminal_pages = True
        self.rows = [dict(row, record_id=rid) for row, rid in zip(self.rows, (910, 911, 810, 8101))]
        self.rows.append(dict(self.rows[-1], id=5, record_id=8102))
        exact = self.rows[2]
        stale = [exact, dict(exact, id=6, build=70576), dict(exact, id=7, build=70577)]
        for page, data in enumerate((stale[:2], stale[2:]), 1):
            self.hotfix_payload_overrides['enUS 100 810 7', page] = dict(
                data=data, total=3, current_page=page, last_page=2, per_page=2)
        with self.assertRaisesRegex(WagoDiffUnavailable, 'prefix sample changed'):
            self.collect()
        self.assertFalse((self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json').exists())

    def test_malformed_speculative_terminal_probe_uses_regular_proof(self):
        self.stable_terminal_pages = True
        self.rows = [dict(row, record_id=rid, build=build) for row, rid, build in zip(
            self.rows, (910, 911, 810, 810), (60575, 60575, 60575, 60576))]
        self.rows.append(dict(self.rows[-1], id=5, build=60577))
        self.hotfix_payload_overrides['enUS 100 810', 1] = dict(
            data=[dict(self.rows[2], build=70575), dict(self.rows[3], build=70576)],
            total=3, current_page=1, last_page=2, per_page=2)
        self.hotfix_payload_overrides['enUS 100 810 7', 1] = None
        self.assertEqual(self.collect(), self.rows)
        self.assertEqual(sum(r['search'] == 'enUS 100 810 7' for r in self.requests), 1)
        self.assertEqual([r['page'] for r in self.requests if r['search'] == 'enUS 100 810 6'], [1, 2])

    def test_terminal_fast_path_reconciles_an_ancestor_that_did_not_use_a_hint(self):
        self.stable_terminal_pages = True
        self.rows = [dict(row, record_id=rid) for row, rid in zip(self.rows, (910, 911, 810, 8101))]
        self.rows.append(dict(self.rows[-1], id=5, record_id=8102))
        stale = [dict(self.rows[2], id=rid, build=70575 + rid) for rid in (6, 7, 8)]
        for query in ('enUS 100 810', 'enUS 100 810 7'):
            self.hotfix_payload_overrides[query, 1] = dict(
                data=stale[:2], total=3, current_page=1, last_page=2, per_page=2)
        self.hotfix_payload_overrides['enUS 100 810 7', 2] = dict(
            data=stale[2:], total=3, current_page=2, last_page=2, per_page=2)
        with self.assertRaisesRegex(WagoDiffUnavailable, r'prefix sample changed.*query=enUS 100 81$'):
            self.collect()
        self.assertFalse((self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json').exists())

    @override_settings(WAGO_HOTFIX_MAX_PREFIX_QUERIES=32)
    def test_speculative_scope_validation_is_atomic_before_normal_fallback(self):
        self.stable_terminal_pages = True
        self.rows = [dict(row, record_id=rid, build=build) for row, rid, build in zip(
            self.rows, (910, 911, 810, 810), (60575, 60575, 60575, 60576))]
        self.rows.append(dict(self.rows[-1], id=5, build=60577))
        hints = [dict(self.rows[2], build=70575), dict(self.rows[3], build=80576)]
        self.hotfix_payload_overrides['enUS 100 810', 1] = dict(
            data=hints, total=3, current_page=1, last_page=2, per_page=2)
        for invalid_fields in ({'record_id': 8101}, {'push_id': float('inf')}):
            with self.subTest(invalid_fields=invalid_fields):
                self.requests.clear()
                self.hotfix_payload_overrides['enUS 100 810 7', 1] = dict(
                    data=[hints[0], dict(hints[1], build=70576, **invalid_fields)], total=2,
                    current_page=1, last_page=1, per_page=2)
                # Use new interval identities so the second replay cannot hit
                # the first replay's complete checkpoint.
                self.assertEqual(self.collect(from_push=98 if 'push_id' in invalid_fields else 99,
                                              monitor=self.monitor()), self.rows)
                queries = [r['search'] for r in self.requests]
                self.assertEqual(queries.count('enUS 100 810 7'), 1)
                self.assertNotIn('enUS 100 810 8', queries)

    def test_inconsistent_root_sample_rejects_cold_and_warm_proofs(self):
        path = self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json'
        for warm in (False, True):
            original = None
            if warm:
                self.assertEqual(self.collect(), self.rows)
                original = path.read_bytes()
            for stale in (dict(self.rows[0], id=999), dict(self.rows[0], data=[810, 'old payload'])):
                with self.subTest(warm=warm, stale=stale):
                    self.root_sample = stale
                    with self.assertRaisesRegex(WagoDiffUnavailable, 'root sample'):
                        self.collect()
                    self.assertEqual(path.read_bytes() if path.exists() else None, original)
            self.root_sample = None

    @override_settings(WAGO_SKILL_DIFF_HTTP_RETRIES=3)
    def test_hotfix_retry_after_blocks_next_scan_and_direct_collection(self):
        state = WowWagoMonitorState.objects.create(
            branch='wow', locale='enUS', build='12.1.5.70575', hotfix_region_id=3,
            hotfix_push_id=99, hotfix_last_run_status='success')
        prefix = ('/hotfixes', 'enUS 100 8')
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.time.sleep') as sleep, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.time.time', return_value=1800000000) as clock:
            self.http_errors[prefix] = [(429, '120')]
            first = self.monitor()
            with patch.object(first, '_resolve_hotfix_facts', return_value=[]), \
                 patch.object(first, '_generate_hotfix_class_report', side_effect=AssertionError('unexpected publication')) as publish, \
                 patch.object(first, '_build_hotfix_fallback_report', return_value=None) as fallback:
                self.assertFalse(first._scan_hotfix_if_needed(state, 'wow', state.build))
            self.assertEqual([r['search'] for r in self.requests], ['enUS % 70575', 'enUS 100', 'enUS 100 8'])
            sleep.assert_not_called()
            publish.assert_not_called()
            fallback.assert_not_called()
            state.refresh_from_db()
            self.assertEqual(state.hotfix_push_id, 99)
            self.assertFalse(WowHotfixReport.objects.exists())

            clock.return_value += 119
            self.requests.clear()
            later = self.monitor()
            with patch.object(later, '_fetch_latest_hotfix_push_id', wraps=later._fetch_latest_hotfix_push_id) as discover:
                self.assertFalse(later._scan_hotfix_if_needed(state, 'wow', state.build))
            discover.assert_not_called()
            with self.assertRaisesRegex(WagoDiffUnavailable, 'HTTP 429'):
                self.collect(later)
            self.assertEqual(self.requests, [])

            # Non-Hotfix transport retains ordinary retries during the cooldown.
            self.http_errors['/builds-diff', 'enUS 100'] = [(503, None), (429, '600')]
            self.assertTrue(later._http_get_text('https://wago.tools/builds-diff?search=enUS+100&page=1'))
            self.assertEqual(len(self.requests), 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

            clock.return_value += 1
            self.requests.clear()
            self.http_errors[prefix] = [(503, None)]
            sleep.reset_mock()
            self.assertEqual(self.collect(later), self.rows)
            self.assertEqual(sum(r['search'] == prefix[1] for r in self.requests), 2)
            sleep.assert_called_once_with(2)

    def test_hotfix_retry_after_http_date_and_missing_header(self):
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.time.time') as clock, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.time.sleep') as sleep:
            clock.return_value = 1800000000
            for header, delay in ((formatdate(1800000180, usegmt=True), 180), (None, 300)):
                with self.subTest(header=header):
                    self.requests.clear()
                    self.http_errors['/hotfixes', 'enUS 100'] = [(429, header)]
                    with self.assertRaisesRegex(WagoDiffUnavailable, 'HTTP 429'):
                        self.collect()
                    self.assertEqual(len(self.requests), 1)
                    clock.return_value += delay - 1
                    with self.assertRaisesRegex(WagoDiffUnavailable, 'HTTP 429'):
                        self.collect()
                    self.assertEqual(len(self.requests), 1)
                    clock.return_value += 1
                    self.assertEqual(self.collect(), self.rows)
            sleep.assert_not_called()

    def test_interrupted_interval_reuses_only_verified_push_proof(self):
        self.fail_push = 101
        with self.assertRaises(WagoDiffUnavailable):
            self.collect(to_push=101)
        cold = [r for r in self.requests if r['search'].split()[1] == '100']
        files = list((self.root / 'var' / 'wago-hotfix-checkpoints').glob('*.json'))
        self.assertEqual(len(files), 1)
        self.requests.clear()
        self.fail_push = None
        self.assertEqual(self.collect(to_push=101), self.rows)
        warm = [r for r in self.requests if r['search'].split()[1] == '100']
        self.assertLess(len(warm), len(cold))
        self.assertEqual([r['search'] for r in warm], ['enUS 100', 'enUS 100 81', 'enUS 100 82'])
        print('WAGO_LOOPBACK_RESUME', json.dumps({'cold_push_requests': len(cold), 'warm_push_requests': len(warm), 'warm_coordinates': warm}))

    def test_same_total_hidden_payload_change_invalidates_checkpoint(self):
        self.collect()
        self.requests.clear()
        # Root always exposes first two rows, so a root-only fingerprint would
        # incorrectly reuse the old fourth row under the same total/source IDs.
        self.rows[-1]['data'][1] = 'changed outside root page'
        self.assertEqual(self.collect(), self.rows)
        self.assertGreater(len(self.requests), 3)
        self.requests.clear()
        self.assertEqual(self.collect(), self.rows)
        self.assertEqual(len(self.requests), 3)

    def test_new_deleted_and_moved_rows_do_not_reuse_stale_proof(self):
        self.collect()
        for mutate in (
            lambda: self.rows.append(dict(self.rows[-1], id=9, record_id=830)),
            lambda: self.rows.pop(0),
            lambda: self.rows[-1].update(record_id=910),
        ):
            with self.subTest(mutation=mutate):
                mutate()
                self.assertEqual(self.collect(), sorted(self.rows, key=lambda r: r['id']))

    def test_scope_range_build_region_locale_and_source_identity_miss(self):
        from botend.services.wago_push_checkpoint import PushCheckpoints
        self.collect()
        cold_count = len(self.requests)
        self.requests.clear()
        self.collect(to_push=101)
        self.assertEqual(sum(r['search'].split()[1] == '100' for r in self.requests), cold_count)
        for kwargs in ({'build_number': 70575}, {'region_id': 1}, {'locale': 'deDE'}):
            with self.subTest(scope=kwargs):
                self.collect()
                self.requests.clear()
                result = self.collect(**kwargs)
                if kwargs.get('locale') == 'deDE':
                    self.assertEqual(result, [])
                    self.assertEqual(self.requests[0]['search'], 'deDE 100')
                else:
                    self.assertEqual(len(self.requests), cold_count)
                    self.assertEqual(result, self.rows if kwargs.get('build_number') else [])
        self.collect()
        cache = PushCheckpoints(from_push=99, to_push=100, region_id=3, locale='enUS', build_number=None, max_pages=40)
        self.assertIsNotNone(cache.load(100))
        cache.scope['source'] = 'https://different.invalid/hotfixes'
        self.assertIsNone(cache.load(100))

    def test_corrupt_checksum_and_truncated_files_are_cache_misses(self):
        self.collect()
        cold_queries = list(self.requests)
        path = self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json'
        for corruption in ('truncated', 'checksum'):
            with self.subTest(corruption=corruption):
                if corruption == 'truncated':
                    path.write_bytes(b'{"body":')
                else:
                    body = json.loads(path.read_bytes())
                    body['body']['proof']['leaves'][0]['rows'][0]['data'] = ['tampered']
                    path.write_text(json.dumps(body))
                self.requests.clear()
                self.assertEqual(self.collect(), self.rows)
                self.assertEqual(self.requests, cold_queries)

    def test_partial_revalidation_never_masks_failed_source_or_updates_checkpoint(self):
        self.collect()
        path = self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json'
        original = path.read_bytes()
        self.fail_push = 100
        with self.assertRaises(WagoDiffUnavailable):
            self.collect()
        self.assertEqual(path.read_bytes(), original)
        self.fail_push = None
        self.assertEqual(self.collect(), self.rows)

    def test_cache_failure_and_budget_limits_do_not_relax_collection(self):
        from botend.services import wago_push_checkpoint as service
        self.collect()
        path = self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json'
        original = path.read_bytes()
        self.rows[-1]['data'][1] = 'new verified payload'
        with patch.object(service, 'atomic_bytes', side_effect=OSError('disk full')):
            self.assertEqual(self.collect(), self.rows)
        self.assertEqual(path.read_bytes(), original)
        with override_settings(WAGO_HOTFIX_MAX_PUSH_SPAN=1):
            before = len(self.requests)
            with self.assertRaisesRegex(WagoDiffUnavailable, 'span limit'):
                self.collect(to_push=101)
            self.assertEqual(len(self.requests), before)
        with override_settings(WAGO_HOTFIX_MAX_ENTRIES=3):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'entry limit'):
                self.collect()

    def test_slot_capacity_replaces_old_scope_and_rejects_links_or_oversize(self):
        from botend.services.wago_push_checkpoint import PushCheckpoints, SLOTS, MAX_BYTES
        cache = PushCheckpoints(from_push=99, to_push=100, region_id=3, locale='enUS', build_number=None, max_pages=40)
        self.collect()
        proof = cache.load(100)
        self.assertTrue(cache.save(100 + SLOTS, proof))
        self.assertIsNone(cache.load(100))
        self.assertIsNotNone(cache.load(100 + SLOTS))
        files = list(cache.root.glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertFalse(cache.save(100, {'total': 1, 'leaves': ['x' * MAX_BYTES]}))
        self.assertIsNotNone(cache.load(100 + SLOTS))
        pending = cache.root / '.100.json.writing-deadwriter'
        pending.write_bytes(b'partial')
        self.assertTrue(cache.save(100, proof))
        self.assertFalse(pending.exists())
        target = self.root / 'do-not-touch'
        target.write_bytes(b'protected')
        files[0].unlink()
        files[0].symlink_to(target)
        self.assertIsNone(cache.load(100))
        self.assertFalse(cache.save(100, proof))
        self.assertEqual(target.read_bytes(), b'protected')

    def test_real_scan_failure_preserves_orm_cursor_and_last_good_report(self):
        state = WowWagoMonitorState.objects.create(
            branch='wow', locale='enUS', build='12.1.5.70575', hotfix_region_id=3,
            hotfix_push_id=99, hotfix_last_run_status='success')
        good = WowHotfixReport.objects.create(branch='wow', locale='enUS', region_id=3,
                                             from_push=98, to_push=99, collection_complete=True,
                                             source_facts_json='["last-good"]')
        self.fail_push = 101
        monitor = self.monitor()
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=101):
            self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', state.build))
        state.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 99)
        self.assertEqual(state.hotfix_last_run_status, 'failed')
        self.assertTrue(good.collection_complete)
        self.assertEqual(good.source_facts_json, '["last-good"]')
        failed = WowHotfixReport.objects.get(region_id=3, to_push=101)
        self.assertFalse(failed.collection_complete)
        self.assertTrue((self.root / 'static' / failed.content_html_path).exists())
        self.assertEqual(len(list((self.root / 'var' / 'wago-hotfix-checkpoints').glob('*.json'))), 1)

    def test_restarted_scan_publishes_complete_facts_then_advances_real_cursor(self):
        state = WowWagoMonitorState.objects.create(
            branch='wow', locale='enUS', build='12.1.5.70575', hotfix_region_id=3,
            hotfix_push_id=99, hotfix_last_run_status='success')
        # Explicit invalidate rows do not assert decoded values. Real facts and
        # HTML renderers run, with unrelated DB2 enrichment unavailable locally.
        for row in self.rows:
            row.update(status=3, data=None, table_name='CheckpointFixture')
        self.fail_push = 101
        first = self.monitor()
        with patch.object(first, '_fetch_latest_hotfix_push_id', return_value=101):
            self.assertFalse(first._scan_hotfix_if_needed(state, 'wow', state.build))
        self.fail_push = None
        self.requests.clear()
        monitor = self.monitor()
        with override_settings(WAGO_HOTFIX_REPORT_ENRICH_MAX=0), \
             patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=101), \
             patch.object(monitor, '_fetch_db2_row_by_id', return_value=None), \
             patch.object(monitor, '_load_chr_classes', return_value={}), \
             patch.object(monitor, '_load_chr_specialization_meta', return_value={}):
            self.assertTrue(monitor._scan_hotfix_if_needed(state, 'wow', state.build))
        state.refresh_from_db()
        report = WowHotfixReport.objects.get(region_id=3, to_push=101)
        self.assertTrue(report.collection_complete)
        self.assertEqual(state.hotfix_push_id, 101)
        self.assertEqual(report.entry_count, len(self.rows))
        self.assertEqual([f['source'] for f in json.loads(report.source_facts_json)], self.rows)
        self.assertTrue((self.root / 'static' / report.content_html_path).is_file())
        self.assertEqual(sum(r['search'].split()[1] == '100' for r in self.requests), 3)
