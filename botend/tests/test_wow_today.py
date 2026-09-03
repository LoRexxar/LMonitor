import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import RequestFactory, SimpleTestCase, TestCase

from LMonitor.config import Monitor_Type_BaseObject_List
from botend.controller.BaseScan import BaseScan
from botend.controller.plugins.wow.WowTodayMonitor import WowTodayMonitor
from botend.models import (
    MonitorTask,
    WowTodayCardSetting,
    WowTodayCardSnapshot,
    WowTodaySectionSetting,
    WowTodaySnapshot,
)
from botend.plugin_sync import monitor_default_wait_time, monitor_task_due_at, portal_data_task_is_due, sync_monitortasks_from_plugin_list
from botend.portal.api import PortalWowTodayAPIView
from botend.services.wow_today_service import (
    WowTodayService,
    WowTodayTranslator,
    extract_today_json,
    select_current_na_roots,
    snapshot_payload_from_html,
)


TODAY_FIXTURE = [
    {
        'id': 'dungeons-and-raids',
        'name': 'Dungeons & Raids',
        'regionId': 'US',
        'groups': [
            {
                'id': 'mythicaffix',
                'name': 'Mythic+ Affixes',
                'type': 'lines',
                'wowExpansion': 11,
                'content': {'lines': [{'name': 'Fortified', 'icon': 'ability_toughness', 'url': '/affix=10/fortified'}]},
            },
            {
                'id': 'tww-notable-world-quests',
                'name': 'Notable World Quests',
                'type': 'lines',
                'wowExpansion': 10,
                'content': {'lines': [{'name': 'Old expansion content'}]},
            },
            {
                'id': 'venomous-abyss-mythic',
                'name': 'The Venomous Abyss (Mythic)',
                'type': 'mythic-progression',
                'wowExpansion': 11,
                'content': {'defeatedBosses': 7, 'totalBosses': 8, 'topGuildCount': 3},
            },
        ],
    },
    {
        'id': 'events-and-rares',
        'name': 'Events & Rares',
        'regionId': 'US',
        'groups': [
            {
                'id': 'holiday',
                'name': 'World Event',
                'type': 'lines',
                'wowIcon': 'calendar_northrendcupstart',
                'content': {'lines': [{'name': 'Northrend Cup', 'url': '/guide/events/northrend-cup-skyriding-races'}]},
            },
        ],
    },
    {
        'id': 'quests-10',
        'name': 'Quests',
        'regionId': 'US',
        'groups': [
            {
                'id': 'tiw-timer',
                'name': 'Daily Quest Reset',
                'type': 'lines',
                'content': {'lines': [{'endingUt': 1788447600}]},
            },
        ],
    },
    {
        'id': 'economy',
        'name': 'Economy',
        'regionId': 'US',
        'groups': [
            {
                'id': 'wow-token',
                'name': 'WoW Token',
                'type': 'token',
                'content': {'priceHtml': '<span>258,815</span>'},
            },
        ],
    },
    {
        'id': 'quests-10',
        'name': 'The War Within',
        'regionId': 'US',
        'groups': [
            {
                'id': 'tww-bountiful-delves',
                'name': 'Bountiful Delves',
                'type': 'lines',
                'wowExpansion': 10,
                'content': {'lines': [{'name': 'Old Delve'}]},
            },
        ],
    },
    {
        'id': 'dungeons-and-raids',
        'name': 'Dungeons & Raids',
        'regionId': 'EU',
        'groups': [
            {
                'id': 'mythicaffix',
                'name': 'Mythic+ Affixes',
                'type': 'lines',
                'wowExpansion': 11,
                'content': {'lines': [{'name': 'Tyrannical'}]},
            },
        ],
    },
]


def fixture_html():
    return '<html><script id="data.wow.todayInWow" type="application/json">{}</script></html>'.format(
        json.dumps(TODAY_FIXTURE)
    )


class UnavailableTranslationService:
    def available(self):
        return False


class FakeResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text


class FakeRequestClient:
    def __init__(self, html_text):
        self.html_text = html_text
        self.calls = []

    def get(self, url, request_type='Resp', *args, **kwargs):
        self.calls.append((url, request_type))
        return FakeResponse(self.html_text)


class WowTodayParserTests(SimpleTestCase):
    def translator(self):
        return WowTodayTranslator(translation_service=UnavailableTranslationService())

    def test_extracts_embedded_json_and_keeps_only_na_current_expansion(self):
        data = extract_today_json(fixture_html())
        roots, expansion_id = select_current_na_roots(data)

        self.assertEqual(expansion_id, 11)
        self.assertEqual(
            [root['name'] for root in roots],
            ['Dungeons & Raids', 'Events & Rares', 'Quests', 'Economy'],
        )
        self.assertEqual([group['id'] for group in roots[0]['groups']], ['mythicaffix'])

    def test_public_payload_is_chinese_and_keeps_stable_module_keys(self):
        payload = snapshot_payload_from_html(fixture_html(), translator=self.translator())

        self.assertEqual(payload['expansion_name'], '午夜之境')
        self.assertEqual(payload['translation_missing'], 0)
        self.assertEqual(payload['sections'][0]['name'], '地下城与团队副本')
        self.assertEqual(payload['sections'][0]['modules'][0]['name'], '大秘境词缀')
        self.assertEqual(payload['sections'][0]['modules'][0]['items'][0]['name'], '强韧')
        self.assertEqual(
            payload['sections'][0]['modules'][0]['items'][0]['icon_url'],
            'https://oss.wowdaily.cn/wow_icons_oss/small/ability_toughness.jpg',
        )
        self.assertEqual(
            payload['sections'][1]['modules'][0]['items'][0]['icon_url'],
            'https://oss.wowdaily.cn/wow_icons_oss/small/calendar_northrendcupstart.jpg',
        )
        self.assertNotIn('烈毒之渊', json.dumps(payload['sections'], ensure_ascii=False))
        self.assertIn('任务', [section['name'] for section in payload['sections']])
        self.assertIn('经济', [section['name'] for section in payload['sections']])
        self.assertNotIn('Old expansion content', json.dumps(payload['sections'], ensure_ascii=False))
        self.assertNotIn('Tyrannical', json.dumps(payload['sections'], ensure_ascii=False))

    def test_daily_schedule_runs_after_na_reset(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = SimpleNamespace(
            name='WowTodayMonitor',
            last_scan_time=datetime(2026, 9, 2, 0, 5, tzinfo=shanghai),
        )

        self.assertFalse(portal_data_task_is_due(task, datetime(2026, 9, 2, 23, 59, tzinfo=shanghai)))
        self.assertTrue(portal_data_task_is_due(task, datetime(2026, 9, 3, 0, 0, tzinfo=shanghai)))
        self.assertEqual(monitor_default_wait_time('WowTodayMonitor'), 86400)
        self.assertIs(Monitor_Type_BaseObject_List[-1], WowTodayMonitor)


class WowTodayPersistenceTests(TestCase):
    def translator(self):
        return WowTodayTranslator(translation_service=UnavailableTranslationService())

    def test_sync_upserts_one_daily_snapshot(self):
        client = FakeRequestClient(fixture_html())
        service = WowTodayService(client, translator=self.translator(), sleep_func=lambda _seconds: None)

        first = service.sync()
        second = service.sync()

        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(WowTodaySnapshot.objects.count(), 1)
        row = WowTodaySnapshot.objects.get()
        self.assertEqual(row.region, 'na')
        self.assertEqual(row.source_region, 'US')
        self.assertEqual(row.expansion_id, 11)
        self.assertEqual(row.sections_json[0]['name'], '地下城与团队副本')
        self.assertEqual(WowTodaySectionSetting.objects.count(), 4)
        self.assertFalse(WowTodaySectionSetting.objects.get(section_key='quests-10').is_visible)
        self.assertFalse(WowTodaySectionSetting.objects.get(section_key='economy').is_visible)
        self.assertEqual(WowTodayCardSnapshot.objects.count(), 4)
        self.assertEqual(WowTodayCardSetting.objects.count(), 4)
        affix_card = WowTodayCardSnapshot.objects.get(section_key='dungeons-and-raids', card_key='mythicaffix')
        self.assertEqual(affix_card.source_name, '大秘境词缀')
        self.assertEqual(affix_card.payload_json['items'][0]['name'], '强韧')
        dungeon_setting = WowTodaySectionSetting.objects.get(section_key='dungeons-and-raids')
        dungeon_setting.display_name = '自定义副本板块'
        dungeon_setting.is_visible = False
        dungeon_setting.sort_order = 90
        dungeon_setting.save()
        service.sync()
        dungeon_setting.refresh_from_db()
        self.assertEqual(dungeon_setting.display_name, '自定义副本板块')
        self.assertFalse(dungeon_setting.is_visible)
        self.assertEqual(dungeon_setting.sort_order, 90)
        card_setting = WowTodayCardSetting.objects.get(
            section_key='dungeons-and-raids', card_key='mythicaffix'
        )
        card_setting.display_name = '每周大秘境词缀'
        card_setting.is_visible = False
        card_setting.sort_order = 80
        card_setting.save()
        service.sync()
        card_setting.refresh_from_db()
        self.assertEqual(card_setting.display_name, '每周大秘境词缀')
        self.assertFalse(card_setting.is_visible)
        self.assertEqual(card_setting.sort_order, 80)
        self.assertEqual(WowTodayCardSnapshot.objects.count(), 4)
        self.assertEqual(client.calls[0][1], 'Response')

    def test_portal_api_exposes_only_public_chinese_snapshot(self):
        WowTodaySnapshot.objects.create(
            snapshot_date=date(2026, 9, 2),
            region='na',
            source_region='US',
            game_version='retail',
            expansion_id=11,
            expansion_name='午夜之境',
            sections_json=[
                {'key': 'events', 'name': '事件与稀有敌人', 'modules': [{'key': 'world-event', 'name': '游戏事件', 'kind': 'lines'}]},
                {'key': 'quests-10', 'name': '任务', 'modules': [{'key': 'tiw-timer', 'name': '每日任务重置', 'kind': 'lines'}]},
                {'key': 'economy', 'name': '经济', 'modules': [{'key': 'wow-token', 'name': '时光徽章', 'kind': 'token'}]},
                {'key': 'raids', 'name': '地下城与团队副本', 'modules': [{'key': 'venomous', 'name': '烈毒之渊（史诗）', 'kind': 'mythic-progression'}]},
            ],
            raw_json=[{'name': 'Events & Rares'}],
        )

        response = PortalWowTodayAPIView.as_view()(RequestFactory().get('/portal/api/today-in-wow/latest/'))
        payload = json.loads(response.content.decode('utf-8'))['data']

        self.assertEqual(payload['region_name'], '北美')
        self.assertEqual(payload['game_version_name'], '正式服')
        self.assertEqual(payload['sections'][0]['name'], '事件与稀有敌人')
        self.assertEqual(len(payload['sections']), 1)
        self.assertNotIn('raw_json', payload)

    def test_plugin_sync_creates_new_daily_task_enabled(self):
        created = sync_monitortasks_from_plugin_list(
            [BaseScan, WowTodayMonitor],
            default_is_active=False,
            default_target='',
            skip_indexes={0},
        )

        self.assertEqual(created, 1)
        task = MonitorTask.objects.get(name='WowTodayMonitor')
        self.assertEqual(task.type, 1)
        self.assertEqual(task.target, 'https://www.wowhead.com/today-in-wow')
        self.assertTrue(task.is_active)
        self.assertTrue(task.proxy_enabled)
        self.assertEqual(task.wait_time, 86400)
        self.assertIsNotNone(monitor_task_due_at(task))


class WowTodayFrontendContractTests(SimpleTestCase):
    def test_panel_is_above_daily_report_and_preferences_are_browser_local(self):
        with open('templates/portal/index.html', 'r', encoding='utf-8') as handle:
            template = handle.read()
        with open('static/portal/js/main.js', 'r', encoding='utf-8') as handle:
            script = handle.read()

        self.assertLess(template.index('id="wow-today-panel"'), template.index('id="featured-news-card"'))
        self.assertIn('wowdaily:today-in-wow:preferences:v1', script)
        self.assertIn('class="portal-tiw-sections"', script)
        self.assertIn('class="portal-tiw-section-grid"', script)
        self.assertIn('portal-tiw-section--pair', script)
        self.assertIn('portal-tiw-section--progress', script)
        self.assertNotIn('class="portal-tiw-rail"', script)
        self.assertNotIn('items.slice(0, 6)', script)
        self.assertIn('localStorage.setItem(WOW_TODAY_PREF_KEY', script)
        self.assertIn('data-tiw-setting', script)
        self.assertIn('data-tiw-setting-legacy', script)
        self.assertIn('wowTodayModulePreferenceKey', script)
        self.assertIn('module?.preference_key', script)
        self.assertIn('当前配置隐藏了全部模块', script)
