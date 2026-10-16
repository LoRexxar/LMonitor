"""聚合查询的权限、分支隔离、过滤和只读契约。"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (DashboardUserGroup, DashboardUserGroupMembership, SeasonMeta,
    WowItemSnapshot, WowItemVariantSnapshot, WowTalentVersion, WowTalentNodeMetadata, WowSpellSnapshot)


class CentralDataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='central-reader')
        cls.group = DashboardUserGroup.objects.create(name='中央资料查询员', permission_codes=['database.central-data'])
        DashboardUserGroupMembership.objects.create(user=cls.user, group=cls.group)
        cls.season = SeasonMeta.objects.create(season_key='query-test', season_name='查询验证赛季',
            mplus_zone_id=1, raid_zone_id=2, gear_batch_key='current')
        cls.item = WowItemSnapshot.objects.create(item_id=990001, name='Test Sword', name_zh='中央长剑',
            slot_key='main_hand', quality=4, icon='inv_sword_01', source='wago', metadata={
                'branch_display': {'ptr': {'name_zh': '测试长剑', 'quality': 3, 'slot_key': 'off_hand'}}})
        for branch, level, track in [('retail', 100, 'hero'), ('retail', 120, 'myth'), ('ptr', 130, 'hero')]:
            WowItemVariantSnapshot.objects.create(item=cls.item, season=cls.season, batch_key='current',
                data_branch=branch, variant_key=f'{branch}-{level}', variant_type='drop_equipment',
                item_level=level, upgrade_track=track, stats_json={'strength': level})
        WowItemVariantSnapshot.objects.create(item=cls.item, season=cls.season, batch_key='old',
            variant_key='old', variant_type='drop_equipment', item_level=999)
        for branch, raw_branch in [('retail', 'wow'), ('ptr', 'wowxptr')]:
            version, _ = WowTalentVersion.objects.update_or_create(key=branch, defaults={
                'branch': branch, 'is_active': True, 'status': 'active', 'current_build': '来源构建'})
            if branch == 'retail':
                cls.retail = version
            for index, spec in enumerate(['Arms', 'Fury']):
                WowTalentNodeMetadata.objects.create(talent_version=version, class_name='Warrior',
                    spec_name=spec, tree_type='spec', node_id=990010 + index, spell_id=990020,
                    name='Test Strike', description_zh='旧节点文本', source='db2_backfill')
            WowSpellSnapshot.objects.create(branch=raw_branch, locale='zhCN', spell_id=990020,
                display_data={'status': 'ready', 'default': {'name': '中央斩击', 'name_zh': '中央斩击',
                    'description_zh': '正式服造成100点伤害' if branch == 'retail' else '测试服造成200点伤害',
                    'icon': 'ability_warrior_savageblow'}, 'contexts': {
                        'warrior/fury': {'description_zh': '狂怒专精追加伤害'}}})
        WowTalentNodeMetadata.all_objects.create(talent_version=cls.retail, class_name='Warrior',
            spec_name='Fury', node_id=990030, spell_id=990031, is_current=False)
        WowTalentNodeMetadata.all_objects.create(talent_version=cls.retail, localization_only=True,
            reference_id=990032, name_zh='仅名称补充')

    def setUp(self):
        self.client.force_login(self.user)
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('查询不得联网采集'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def query(self, **params):
        response = self.client.get('/api/dashboard/central-data/', params)
        self.assertEqual(response.status_code, 200, response.content.decode())
        return response.json()

    def test_dedicated_permission_controls_page_and_api(self):
        page = self.client.get('/dashboard/?section=central-data')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="central-data-workspace"')
        self.assertContains(page, 'central-data.js')
        self.assertEqual(page['Cache-Control'], 'private, no-store')
        self.group.permission_codes = ['database.tables']
        self.group.save()
        self.assertEqual(self.client.get('/dashboard/?section=central-data').status_code, 403)
        self.assertEqual(self.client.get('/api/dashboard/central-data/').status_code, 403)
        self.client.logout()
        self.assertIn(self.client.get('/api/dashboard/central-data/').status_code, (302, 403))

    def test_items_group_current_variants_and_never_mix_branches(self):
        result = self.query()
        self.assertEqual(result['pagination']['total'], 1)
        row = result['records'][0]
        self.assertEqual((row['id'], row['name'], row['count'], row['minimum'], row['maximum']),
            (990001, '中央长剑', 2, 100, 120))
        ptr = self.query(branch='ptr', q='测试长剑', quality=3, slot='off_hand')['records'][0]
        self.assertEqual((ptr['name'], ptr['count'], ptr['minimum']), ('测试长剑', 1, 130))
        self.assertEqual(self.query(q='测试长剑')['pagination']['total'], 0)

    def test_filters_apply_to_same_variant(self):
        self.assertEqual(self.query(upgrade_track='hero', min_level=110)['pagination']['total'], 0)
        self.assertEqual(self.query(upgrade_track='myth', min_level=110, max_level=125,
            season=self.season.pk, variant_type='drop_equipment', quality=4, slot='main_hand',
            language='translated', q='990001')['records'][0]['count'], 1)
        self.assertEqual(self.query(language='missing')['pagination']['total'], 0)
        self.assertEqual(self.query(quality=0)['pagination']['total'], 0)
        self.assertEqual(self.query(q='Test Sword')['pagination']['total'], 1)

    def test_item_detail_and_missing_identity(self):
        result = self.query(id=990001, branch='ptr')
        self.assertEqual(result['record']['name'], '测试长剑')
        self.assertEqual(result['entries'][0]['stats'], {'strength': 130})
        self.assertIn('统一图标服务', result['record']['icon_source'])
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(self.client.get('/api/dashboard/central-data/', {'id': 990099}).status_code, 404)

    def test_actual_drop_quality_is_used_for_filters_and_details(self):
        WowItemVariantSnapshot.objects.filter(item=self.item, item_level=100).update(
            metadata={'difficulty_drop': {'quality': 3}})
        result = self.query(quality=3)
        self.assertEqual(result['records'][0]['count'], 1)
        self.assertEqual(result['records'][0]['quality'], '精良')
        detail = self.query(id=990001, quality=3)
        self.assertEqual(detail['entries'][0]['quality'], '精良')
        self.assertEqual(detail['entries'][0]['item_level'], 100)

    def test_confirmed_empty_effects_share_the_existing_provenance_check(self):
        with patch('botend.dashboard.central_data._native_journal_variant', return_value=True):
            result = self.query(id=990001)
        self.assertTrue(all(entry['effects_status'] == 'none' for entry in result['entries']))

    def test_online_icon_provenance_fixes_reach_aggregated_page(self):
        self.item.metadata['branch_display']['ptr']['icon'] = '8268560'
        self.item.save()
        row = self.query(branch='ptr')['records'][0]
        self.assertIn('/large/inv_sword_01.jpg', row['icon_url'])
        self.item.icon = '8268560'
        self.item.source = 'wago'
        self.item.save()
        self.assertEqual(self.query(branch='ptr')['records'][0]['icon_url'], '')
        self.item.source = 'wowhead_live_tooltip'
        self.item.save()
        self.assertIn('/large/8268560.jpg', self.query(branch='ptr')['records'][0]['icon_url'])

    def test_online_chinese_effect_templates_reach_aggregated_page(self):
        self.item.item_id = 281029
        self.item.save()
        raw = [{'description': "Equip: Your attacks have a chance to amplify the band's whispers, calling forth an Acidbrood Scourge. The scourge flies into your target and detonates, causing (1 * 3748) Nature damage to all nearby enemies."}]
        WowItemVariantSnapshot.objects.filter(item=self.item, data_branch='ptr').update(effects_json=raw)
        result = self.query(id=281029, branch='ptr')
        effect = result['entries'][0]['effects'][0]
        self.assertIn('酸巢灾虫', effect['description_zh'])
        self.assertIn('(1 * 3748)', effect['description_zh'])
        self.assertEqual(result['entries'][0]['effects_status'], 'localized')
        self.assertEqual(WowItemVariantSnapshot.objects.get(item=self.item, data_branch='ptr').effects_json, raw)

    def test_talents_group_spells_and_use_final_chinese(self):
        result = self.query(resource='talents', q='中央斩击')
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(result['records'][0]['count'], 2)
        self.assertEqual(result['records'][0]['description'], '正式服造成100点伤害')
        detail = self.query(resource='talents', id=990020)
        self.assertEqual(detail['record']['central']['description_zh'], '正式服造成100点伤害')
        self.assertEqual({entry['description'] for entry in detail['entries']},
            {'正式服造成100点伤害', '狂怒专精追加伤害'})
        ptr = self.query(resource='talents', branch='ptr', q='200点伤害')
        self.assertEqual(ptr['records'][0]['description'], '测试服造成200点伤害')
        self.assertEqual(self.query(resource='talents', q='200点伤害')['pagination']['total'], 0)

    def test_talent_filters_and_status(self):
        result = self.query(resource='talents', class_name='Warrior', spec_name='Fury', tree_type='spec', status='ready', q=990011)
        self.assertEqual(result['records'][0]['count'], 1)
        self.assertEqual(result['records'][0]['description'], '狂怒专精追加伤害')
        self.assertEqual(self.query(resource='talents', status='partial')['pagination']['total'], 0)
        self.assertEqual(self.query(resource='talents', status='unbuilt')['pagination']['total'], 0)

    def test_anchor_does_not_borrow_spell_and_unavailable_branch_stays_empty(self):
        WowTalentNodeMetadata.objects.create(talent_version=self.retail, class_name='Warrior',
            spec_name='Fury', tree_type='hero_anchor', node_id=990020, spell_id=990020, name_zh='英雄入口')
        result = self.query(resource='talents', tree_type='hero_anchor', status='unbuilt')
        self.assertEqual(result['records'][0]['identity_kind'], 'node')
        detail = self.query(resource='talents', id=990020, identity_kind='node')
        self.assertEqual(detail['record']['central'], {})
        self.assertIsNone(detail['record']['spell_id'])
        WowTalentVersion.objects.filter(branch='ptr').update(is_active=False)
        self.assertEqual(self.query(resource='talents', branch='ptr')['pagination']['total'], 0)

    def test_invalid_parameters_and_mutations(self):
        for params in ({'branch': 'beta'}, {'resource': 'other'}, {'page_size': 200}, {'page': 'bad'},
                       {'min_level': 200, 'max_level': 100}, {'quality': -1}, {'language': 'bad'},
                       {'resource': 'talents', 'status': 'bad'}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get('/api/dashboard/central-data/', params).status_code, 400)
        for method in ('post', 'put', 'patch', 'delete'):
            self.assertEqual(getattr(self.client, method)('/api/dashboard/central-data/').status_code, 405)

    def test_pagination_and_no_writes_or_parsing(self):
        for index in range(11):
            item = WowItemSnapshot.objects.create(item_id=991000 + index, name_zh=f'分页装备{index}')
            WowItemVariantSnapshot.objects.create(item=item, season=self.season, batch_key='current',
                variant_key=str(index), variant_type='drop_equipment')
        with CaptureQueriesContext(connection) as queries, patch(
                'botend.wow.spell_text.SpellTextResolver.resolve', side_effect=AssertionError('不得重新解析')):
            result = self.query(page_size=10, page=2)
            self.query(resource='talents')
            self.query(resource='talents', id=990020)
        self.assertEqual(result['pagination']['total'], 12)
        self.assertEqual(len(result['records']), 2)
        self.assertFalse(any(query['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for query in queries))
