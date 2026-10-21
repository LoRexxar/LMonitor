"""Report denial excludes observations, never partial event streams."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
from django.test import SimpleTestCase

from botend.services import raid_burst_aggregation as agg
from botend.services.raid_burst_events import EventFetchDeferred, cache_path, fetch_events
from botend.services.raid_burst_snapshot import read_blob, read_json
from botend.services.wcl_api_budget import _error
from botend.tests.test_raid_burst_aggregation import EventClient


class ReportPermissionTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.row = dict(report_code='Public', fight_id=1, character_name='测试玩家', realm='测试服',
                        region='CN', class_name='Warrior', spec_name='Arms', boss_id=3470, difficulty=5, dps=123)
        self.scope = dict(season_id=1, class_name='Warrior', spec_name='Arms', boss_id=3470,
                          difficulty=5, key='1/Warrior/Arms/5/3470', label='测试')
        self.client = EventClient()

    def build(self, rows):
        result = agg.build_scope(rows, self.scope, agg.SUPPORTED[('Warrior', 'Arms')],
                                 self.root, nullcontext, self.client)
        return read_blob(self.root / '1/Warrior/Arms', result['blob'])

    def denial(self):
        return _error({'errors': [{'message': 'You do not have permission to view this report.',
                                  'path': ['reportData', 'report']}]})

    def test_denial_at_metadata_or_mid_page_keeps_counts_and_other_cached_samples(self):
        for mid_page in (False, True):
            with self.subTest(mid_page=mid_page):
                self.client.failure = None
                fetch_events(self.root, self.row, self.client, nullcontext)
                good_bytes = cache_path(self.root, self.row).read_bytes()
                self.client.calls.clear()
                # Three distinct ranked player identities: this test exercises
                # permission recovery, not the separate duplicate-player filter.
                private = dict(self.row, report_code=f'Private{mid_page}', region='US')
                following = dict(self.row, report_code=f'Following{mid_page}', region='EU')
                self.client.paginate = mid_page
                self.client._wcl_last_error = self.denial()
                self.client.failure = lambda v: v['code'] == private['report_code'] and (
                    not mid_page or (v.get('kind') == 'Casts' and v.get('start') == 130000))
                payload = self.build([self.row, private, following])
                self.assertEqual((payload['attempted'], payload['successful'], payload['excluded_invalid']), (3, 2, 1))
                self.assertEqual(len(payload['samples']), 2)
                self.assertEqual(cache_path(self.root, self.row).read_bytes(), good_bytes)
                self.assertFalse(any(v['code'] == 'Public' for v in self.client.calls))
                raw = fetch_events(self.root, private, self.client, nullcontext)
                self.assertEqual(raw, {'invalid': '日志权限拒绝（permission）'})
                self.assertNotIn('streams', raw)
                if mid_page:
                    partial = read_json(cache_path(self.root, private))['data']['streams']['casts']
                    self.assertFalse(partial['complete'])
                    self.assertEqual(partial['cursor'], 130000)
                    self.client.failure = None
                    self.client.calls.clear()
                    recovered = fetch_events(self.root, private, self.client, nullcontext)
                    self.assertEqual(self.client.calls[0]['start'], 130000)
                    self.assertEqual(len(recovered['streams']['casts']['data']), 3)
                else:
                    self.assertFalse(cache_path(self.root, private).exists())

    def test_other_failures_still_defer_at_metadata_and_mid_page(self):
        errors = ['budget_wait', 'authentication', 'http_403', 'rate_limit', 'network', 'graphql',
                  _error({'errors': [{'message': 'permission to view this report', 'path': ['other']}]}),
                  _error({'errors': [{'message': 'Access denied', 'path': ['reportData', 'report']}]})]
        for mid_page in (False, True):
            for index, error in enumerate(errors):
                with self.subTest(error=error, mid_page=mid_page):
                    row = dict(self.row, report_code=f'Error{mid_page}{index}')
                    self.client.paginate = mid_page
                    self.client._wcl_last_error = error
                    self.client.failure = lambda v: not mid_page or (v.get('kind') == 'Casts' and v.get('start') == 130000)
                    with self.assertRaises(EventFetchDeferred) as caught:
                        fetch_events(self.root, row, self.client, nullcontext)
                    self.assertEqual(str(caught.exception), error)
                    self.assertEqual(caught.exception.retry_at, self.client._wcl_retry_at)

    def test_lost_owner_before_request_is_not_an_unavailable_sample(self):
        self.client._wcl_last_error = self.denial()
        self.client.failure = lambda v: True
        with self.assertRaisesRegex(RuntimeError, 'owner lost'):
            fetch_events(self.root, self.row, self.client, lambda: (_ for _ in ()).throw(RuntimeError('owner lost')))
        self.assertEqual(self.client.calls, [])
