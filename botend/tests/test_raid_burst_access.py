"""战斗统计的匿名隔离、登录入口及私有数据回归。"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse

from botend.portal.raid_burst import PortalRaidBurstView
from botend.services.raid_burst_snapshot import load_published, digest, write_json
from botend.services.raid_burst_aggregation import build_groups, SUPPORTED
from botend.portal.views import PortalHomeView


class RaidBurstAccessTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.url = reverse('portal_raid_burst')
        self.root = Path(tempfile.mkdtemp(prefix='raid-burst-access-'))
        override = override_settings(RAID_BURST_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        payload = json.loads((Path(__file__).resolve().parents[1] / 'data' / 'raid_burst_preview.json').read_text(encoding='utf8'))
        payload.update(schema=1, scope={'key': '1/Warrior/Arms/5/3470', 'label': '测试首领'},
                       generated_at='2026-10-20', excluded_invalid=0,
                       skill_names=SUPPORTED[('Warrior', 'Arms')]['spells'])
        payload['groups'] = build_groups(payload['samples'], SUPPORTED[('Warrior', 'Arms')]['spells'])
        directory = self.root / '1/Warrior/Arms'
        blob = digest(payload)
        write_json(directory / 'snapshots' / f'{blob}.json', payload)
        manifest = {'schema': 1, 'scopes': [{'key': payload['scope']['key'], 'label': '测试首领', 'season_id': 1, 'blob': blob}]}
        write_json(directory / 'index.json', {'data': manifest, 'digest': digest(manifest)})

    def request(self, authenticated=False, method='get'):
        request = getattr(self.factory, method)(self.url)
        request.user = User(username='普通测试用户', is_staff=False) if authenticated else AnonymousUser()
        return request

    def test_anonymous_cannot_read_page_or_payload(self):
        with patch('botend.portal.raid_burst.load_published') as loader:
            for method in ('get', 'head'):
                with self.subTest(method=method):
                    response = resolve(self.url).func(self.request(method=method))
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.url, '/auth/login/?next=/portal/raid-burst/')
                    self.assertNotIn(b'raid-burst-data', response.content)
                    self.assertIn('no-store', response['Cache-Control'])
                    self.assertIn('Cookie', response['Vary'])
            loader.assert_not_called()

    def test_normal_account_can_read_without_dashboard_permission(self):
        response = PortalRaidBurstView.as_view()(self.request(authenticated=True))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="raid-burst-data"')
        self.assertContains(response, 'id="portal-raid-burst-entry"')
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('Cookie', response['Vary'])
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_header_only_renders_button_for_authenticated_user(self):
        anonymous = render_to_string('portal/_header.html', request=self.request())
        authenticated = render_to_string('portal/_header.html', request=self.request(authenticated=True))
        self.assertNotIn(self.url, anonymous)
        self.assertNotIn('战斗统计', anonymous)
        self.assertIn('id="portal-raid-burst-entry"', authenticated)
        self.assertIn(self.url, authenticated)

    def test_public_home_does_not_embed_battle_data(self):
        for authenticated in (False, True):
            response = PortalHomeView.as_view()(self.request(authenticated=authenticated))
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'raid-burst-data', response.content)
            self.assertNotIn(b'lust_windows', response.content)

    def test_logout_state_does_not_reuse_authenticated_response(self):
        authenticated = PortalRaidBurstView.as_view()(self.request(authenticated=True))
        anonymous = PortalRaidBurstView.as_view()(self.request())
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(anonymous.status_code, 302)
        self.assertNotIn(b'raid-burst-data', anonymous.content)

    def test_snapshot_has_no_unqualified_or_raw_records(self):
        payload, _ = load_published()
        self.assertEqual(len(payload['samples']), 8)
        self.assertEqual({s['lust_group'] for s in payload['samples']}, {'opener', 'phase3'})
        for sample in payload['samples']:
            self.assertTrue(sample['incoming_complete'])
            self.assertFalse(sample['pi_observed'])
            self.assertIsNotNone(sample['lust_start'])
            self.assertNotIn('report', sample)
            self.assertNotIn('character_name', sample)
            self.assertTrue(sample['player']['name'])
            self.assertTrue(sample['player']['realm'])
            self.assertGreater(sample['dps'], 0)
        witness = next(sample for sample in payload['samples'] if sample['rank'] == 10)
        self.assertEqual(witness['player'], {'name': 'Zitee', 'realm': 'Kazzak', 'region': 'EU'})
        self.assertAlmostEqual(witness['dps'], 254872.44049856)
        self.assertIn('qwt3G8vy6pBa4PWN?fight=12&type=casts&source=292', witness['url'])
        self.assertIsNone(finders.find('raid_burst_preview.json'))

    def test_missing_or_corrupt_snapshot_never_fetches_or_aggregates(self):
        directory = self.root / '1/Warrior/Arms'
        (directory / 'index.json').write_text('{', encoding='utf8')
        with patch('botend.services.raid_burst_aggregation.build_scope', side_effect=AssertionError('页面重算')), \
                patch('botend.services.raid_burst_events.fetch_events', side_effect=AssertionError('页面抓取')):
            response = PortalRaidBurstView.as_view()(self.request(authenticated=True))
        self.assertContains(response, '暂无已生成的战斗统计')
        self.assertNotIn(b'lust_windows', response.content)
