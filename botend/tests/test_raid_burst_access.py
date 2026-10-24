"""战斗统计的匿名隔离、登录入口及私有数据回归。"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from botend.portal.raid_burst import PortalRaidBurstView
from botend.services.raid_burst_snapshot import load_published, digest, write_json
from botend.services.raid_burst_aggregation import build_groups, SUPPORTED
from botend.portal.views import PortalHomeView


class RaidBurstAccessTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create(username="普通测试用户", is_staff=False)
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

    def request(self, authenticated=False, method='get', scope='1/Warrior/Arms/5/3470'):
        request = getattr(self.factory, method)(self.url, {'scope': scope} if authenticated else {})
        request.user = self.user if authenticated else AnonymousUser()
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
        self.assertNotContains(response, 'id="portal-raid-burst-entry"')
        self.assertContains(response, '测试首领 · 战斗统计')
        self.assertNotContains(response, 'id="scope"')
        self.assertContains(response, '/portal/spec/Warrior/Arms/raid/?boss_id=3470&amp;difficulty=5')
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('Cookie', response['Vary'])
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_manual_update_labels_do_not_claim_old_payload_is_top10(self):
        response = PortalRaidBurstView.as_view()(self.request(authenticated=True))
        self.assertContains(response, '独立手动更新')
        self.assertContains(response, '当前显示历史采样版本')
        self.assertNotContains(response, '每日团本统计')
        self.assertNotContains(response, '请等待团本每日任务完成')

    def test_top10_policy_is_rendered_only_from_published_payload(self):
        payload, _ = load_published('1/Warrior/Arms/5/3470')
        assert payload is not None
        payload.update(selection_limit=10, candidate_count=100, excluded_duplicate=2)
        directory = self.root / '1/Warrior/Arms'
        signature = digest(payload)
        write_json(directory / 'snapshots' / f'{signature}.json', payload)
        manifest = {'schema': 1, 'scopes': [{'key': payload['scope']['key'],
                    'label': '测试首领', 'season_id': 1, 'blob': signature}]}
        write_json(directory / 'index.json', {'data': manifest, 'digest': digest(manifest)})
        response = PortalRaidBurstView.as_view()(self.request(authenticated=True))
        self.assertContains(response, '最多保留前10位可用且不重复的玩家')
        self.assertContains(response, '每日自动更新已关闭')
        self.assertNotContains(response, '当前显示历史采样版本')

    def test_global_header_never_renders_battle_entry(self):
        for authenticated in (False, True):
            header = render_to_string('portal/_header.html', request=self.request(authenticated))
            self.assertNotIn(self.url, header)
            self.assertNotIn('战斗统计', header)

    def test_only_logged_in_boss_detail_has_entry_with_matching_scope(self):
        for authenticated in (False, True):
            for boss_detail in (None, {'boss_id': 3470, 'boss_name': '测试首领'}):
                for difficulty in (4, 5):
                    context = {'season': SimpleNamespace(id=7), 'class_name': 'Warrior',
                               'spec_name': 'Arms', 'selected_raid_difficulty': difficulty,
                               'boss_detail': boss_detail}
                    html = render_to_string('portal/spec_detail/raid_stats.html', context,
                                            request=self.request(authenticated))
                    with self.subTest(authenticated=authenticated, boss=bool(boss_detail), difficulty=difficulty):
                        if authenticated and boss_detail:
                            self.assertEqual(html.count('id="portal-raid-burst-entry"'), 1)
                            self.assertIn(f'{self.url}?scope=7/Warrior/Arms/{difficulty}/3470', html)
                        else:
                            self.assertNotIn('id="portal-raid-burst-entry"', html)

    def test_public_home_does_not_embed_battle_data(self):
        for authenticated in (False, True):
            response = PortalHomeView.as_view()(self.request(authenticated=authenticated))
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'raid-burst-data', response.content)
            self.assertNotIn(b'lust_windows', response.content)
            self.assertNotIn(b'portal-raid-burst-entry', response.content)

    def test_logout_state_does_not_reuse_authenticated_response(self):
        authenticated = PortalRaidBurstView.as_view()(self.request(authenticated=True))
        anonymous = PortalRaidBurstView.as_view()(self.request())
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(anonymous.status_code, 302)
        self.assertNotIn(b'raid-burst-data', anonymous.content)

    def test_snapshot_has_no_unqualified_or_raw_records(self):
        payload, _ = load_published('1/Warrior/Arms/5/3470')
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

    def test_missing_scope_never_defaults_to_another_boss(self):
        with patch('botend.portal.raid_burst.load_published') as loader:
            for scope in ('', 'invalid', '1/Warrior/Arms/3/3470'):
                response = PortalRaidBurstView.as_view()(self.request(True, scope=scope))
                self.assertContains(response, '请从职业团本聚合的具体首领详情页进入', status_code=400)
                self.assertNotIn(b'lust_windows', response.content)
            loader.assert_not_called()
        self.assertIsNone(load_published()[0])

    def test_other_boss_difficulty_spec_or_season_never_uses_available_snapshot(self):
        for scope in ('1/Warrior/Arms/5/3471', '1/Warrior/Arms/4/3470',
                      '1/Warrior/Fury/5/3470', '2/Warrior/Arms/5/3470'):
            with self.subTest(scope=scope):
                response = PortalRaidBurstView.as_view()(self.request(True, scope=scope))
                self.assertContains(response, '本首领当前专精与难度暂无已生成的战斗统计')
                self.assertNotIn(b'lust_windows', response.content)
                self.assertNotContains(response, '测试首领')
