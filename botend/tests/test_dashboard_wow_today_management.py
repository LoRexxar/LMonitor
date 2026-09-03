import json
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.models import (
    DashboardUserGroup,
    DashboardUserGroupMembership,
    WowTodayCardSetting,
    WowTodaySectionSetting,
    WowTodaySnapshot,
)


User = get_user_model()
ROOT = Path(__file__).resolve().parents[2]


def snapshot_sections():
    return [
        {
            'key': 'dungeons-raids',
            'name': '地下城与团队副本',
            'modules': [
                {'key': 'affixes', 'name': '大秘境词缀', 'kind': 'lines', 'items': [{'name': '强韧'}]},
                {'key': 'venomous', 'name': '烈毒之渊（史诗）', 'kind': 'mythic-progression', 'metrics': {'total_bosses': 8}},
            ],
        },
        {
            'key': 'events-rares',
            'name': '事件与稀有敌人',
            'modules': [{'key': 'world-event', 'name': '游戏事件', 'kind': 'lines', 'items': [{'name': '暗月马戏团'}]}],
        },
        {
            'key': 'quests-10',
            'name': '任务',
            'modules': [{'key': 'quest-reset', 'name': '每日任务重置', 'kind': 'lines', 'items': [{'name': '每日任务重置'}]}],
        },
        {
            'key': 'economy',
            'name': '经济',
            'modules': [{'key': 'wow-token', 'name': '时光徽章', 'kind': 'token', 'items': [{'name': '时光徽章', 'value': '258,815'}]}],
        },
    ]


class DashboardWowTodayManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='wow-today-admin', email='wow-today-admin@example.com', password='Admin-pass-123!'
        )
        self.viewer = User.objects.create_user(username='wow-today-viewer', password='Viewer-pass-123!')
        self.snapshot = WowTodaySnapshot.objects.create(
            snapshot_date=date(2026, 9, 3),
            region='na',
            source_region='US',
            game_version='retail',
            expansion_id=11,
            expansion_name='午夜之境',
            sections_json=snapshot_sections(),
        )

    def grant_viewer_permission(self):
        group = DashboardUserGroup.objects.create(
            name='今日魔兽内容管理员',
            permission_codes=['reports.wow-today-settings'],
            is_active=True,
        )
        DashboardUserGroupMembership.objects.create(user=self.viewer, group=group)

    def test_dashboard_page_and_api_require_matching_permission(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get('/dashboard/?section=wow-today-settings').status_code, 403)
        self.assertEqual(self.client.get('/api/dashboard/wow-today-sections/').status_code, 403)

        self.grant_viewer_permission()
        page = self.client.get('/dashboard/?section=wow-today-settings')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['dashboard_default_section'], 'wow-today-settings')
        self.assertContains(page, '今日魔兽编排')
        self.assertEqual(self.client.get('/api/dashboard/wow-today-sections/').status_code, 200)

    def test_get_discovers_sections_and_applies_recommended_defaults(self):
        self.client.force_login(self.admin)
        response = self.client.get('/api/dashboard/wow-today-sections/')

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload['summary'], {
            'section_total': 4,
            'section_visible': 2,
            'card_total': 4,
            'card_visible': 4,
            'card_effective_visible': 2,
        })
        self.assertEqual([row['key'] for row in payload['records']], [
            'dungeons-raids', 'events-rares', 'quests-10', 'economy',
        ])
        self.assertEqual(WowTodaySectionSetting.objects.count(), 4)
        self.assertFalse(WowTodaySectionSetting.objects.get(section_key='quests-10').is_visible)
        self.assertFalse(WowTodaySectionSetting.objects.get(section_key='economy').is_visible)
        dungeon = payload['records'][0]
        self.assertEqual(dungeon['card_count'], 1)
        self.assertEqual(dungeon['cards'][0]['key'], 'affixes')
        self.assertEqual(dungeon['cards'][0]['preview_items'], ['强韧'])
        self.assertEqual(WowTodayCardSetting.objects.count(), 4)

    def test_patch_updates_name_visibility_order_and_public_projection(self):
        self.client.force_login(self.admin)
        self.client.get('/api/dashboard/wow-today-sections/')
        response = self.client.patch(
            '/api/dashboard/wow-today-sections/',
            data=json.dumps({
                'sections': [
                    {
                        'key': 'quests-10', 'display_name': '每日任务精选', 'is_visible': True,
                        'cards': [{'key': 'quest-reset', 'display_name': '重置倒计时', 'is_visible': True}],
                    },
                    {
                        'key': 'dungeons-raids', 'display_name': '', 'is_visible': True,
                        'cards': [{'key': 'affixes', 'display_name': '本周词缀', 'is_visible': True}],
                    },
                    {
                        'key': 'events-rares', 'display_name': '', 'is_visible': True,
                        'cards': [{'key': 'world-event', 'display_name': '', 'is_visible': False}],
                    },
                    {
                        'key': 'economy', 'display_name': '', 'is_visible': False,
                        'cards': [{'key': 'wow-token', 'display_name': '', 'is_visible': True}],
                    },
                ],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['summary']['card_effective_visible'], 2)
        self.assertEqual(WowTodaySectionSetting.objects.get(section_key='quests-10').display_name, '每日任务精选')
        self.assertEqual(
            WowTodayCardSetting.objects.get(section_key='quests-10', card_key='quest-reset').display_name,
            '重置倒计时',
        )
        self.assertFalse(
            WowTodayCardSetting.objects.get(section_key='events-rares', card_key='world-event').is_visible
        )

        public_payload = self.client.get('/portal/api/today-in-wow/latest/').json()['data']
        self.assertEqual(
            [section['name'] for section in public_payload['sections']],
            ['每日任务精选', '地下城与团队副本'],
        )
        self.assertEqual(public_payload['sections'][0]['modules'][0]['name'], '重置倒计时')
        self.assertEqual(public_payload['sections'][1]['modules'][0]['name'], '本周词缀')
        self.assertEqual(
            public_payload['sections'][1]['modules'][0]['preference_key'],
            'dungeons-raids/affixes',
        )
        self.assertNotIn('烈毒之渊', json.dumps(public_payload, ensure_ascii=False))
        self.assertNotIn('事件与稀有敌人', json.dumps(public_payload, ensure_ascii=False))

    def test_patch_rejects_stale_or_invalid_complete_list(self):
        self.client.force_login(self.admin)
        incomplete = self.client.patch(
            '/api/dashboard/wow-today-sections/',
            data=json.dumps({'sections': [
                {
                    'key': 'dungeons-raids', 'display_name': '', 'is_visible': True,
                    'cards': [{'key': 'affixes', 'display_name': '', 'is_visible': True}],
                },
            ]}),
            content_type='application/json',
        )
        self.assertEqual(incomplete.status_code, 409)

        invalid = self.client.patch(
            '/api/dashboard/wow-today-sections/',
            data=json.dumps({'sections': [
                {
                    'key': 'dungeons-raids', 'display_name': '', 'is_visible': True,
                    'cards': [{'key': 'affixes', 'display_name': '', 'is_visible': 'yes'}],
                },
                {
                    'key': 'events-rares', 'display_name': '', 'is_visible': True,
                    'cards': [{'key': 'world-event', 'display_name': '', 'is_visible': True}],
                },
                {
                    'key': 'quests-10', 'display_name': '', 'is_visible': False,
                    'cards': [{'key': 'quest-reset', 'display_name': '', 'is_visible': True}],
                },
                {
                    'key': 'economy', 'display_name': '', 'is_visible': False,
                    'cards': [{'key': 'wow-token', 'display_name': '', 'is_visible': True}],
                },
            ]}),
            content_type='application/json',
        )
        self.assertEqual(invalid.status_code, 400)


class DashboardWowTodayFrontendContractTests(TestCase):
    def test_dashboard_contains_responsive_section_editor_without_wide_table(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        javascript = (ROOT / 'static/dashboard/js/wow_today_management.js').read_text(encoding='utf-8')
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')

        self.assertIn('data-section="wow-today-settings"', template)
        self.assertIn("dashboard/js/wow_today_management.js", template)
        section = template[template.index('id="wow-today-settings"'):template.index('id="wago-hotfix-reports"')]
        self.assertNotIn('overflow-x-auto', section)
        self.assertNotIn('<table', section)
        self.assertIn('data-wow-today-card-key', javascript)
        self.assertIn('data-wow-today-card-name', javascript)
        self.assertIn('data-wow-today-card-visible', javascript)
        self.assertIn('card.preview_items', javascript)
        self.assertIn('sectionItem.cards.map', javascript)
        self.assertIn('window.loadWowTodaySectionSettings', javascript)
        self.assertIn("method: 'PATCH'", javascript)
        self.assertIn("sectionId === 'wow-today-settings'", main)
