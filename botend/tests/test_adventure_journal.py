"""冒险手册难度、职责、同步原子性及公开页面回归。"""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.services.journal_service import allowed_difficulties, compile_journal, section_tree, sync_journal
from botend.services.journal_source import TABLES, WagoJournalSource, latest_retail_build
from botend.services.journal_text import JournalText, difficulty_text
from botend.services.journal_items import supplement_items


def fixture():
    tables = {name: [] for name in TABLES}
    tables.update({
        'JournalInstance': [{'ID': '10', 'Name_lang': '测试副本', 'Description_lang': '副本背景', 'MapID': '20'}],
        'JournalEncounter': [{'ID': '30', 'JournalInstanceID': '10', 'Name_lang': '测试首领',
                              'Description_lang': '首领背景', 'OrderIndex': '1', 'FirstSectionID': '40', 'Flags': '0'}],
        'JournalEncounterSection': [
            {'ID': '40', 'JournalEncounterID': '30', 'Title_lang': '坦克', 'BodyText_lang': '交替承受重击',
             'FirstChildSectionID': '0', 'NextSiblingSectionID': '41', 'Type': '3', 'IconFlags': '1', 'Flags': '0', 'SpellID': '0'},
            {'ID': '41', 'JournalEncounterID': '30', 'Title_lang': '第一阶段', 'BodyText_lang': '躲避地面伤害',
             'FirstChildSectionID': '42', 'NextSiblingSectionID': '43', 'Type': '0', 'IconFlags': '0', 'Flags': '0', 'SpellID': '0'},
            {'ID': '42', 'JournalEncounterID': '30', 'Title_lang': '英雄技能', 'BodyText_lang': '英雄特有机制',
             'FirstChildSectionID': '0', 'NextSiblingSectionID': '0', 'Type': '2', 'IconFlags': '4', 'Flags': '2', 'SpellID': '100'},
            {'ID': '43', 'JournalEncounterID': '30', 'Title_lang': '治疗者', 'BodyText_lang': '及时抬血',
             'FirstChildSectionID': '0', 'NextSiblingSectionID': '0', 'Type': '3', 'IconFlags': '4', 'Flags': '0', 'SpellID': '0'},
        ],
        'JournalEncounterItem': [
            {'ID': '50', 'JournalEncounterID': '30', 'ItemID': '60', 'Flags': '2', 'DifficultyMask': '1', 'FactionMask': '-1'},
            {'ID': '51', 'JournalEncounterID': '30', 'ItemID': '61', 'Flags': '2', 'DifficultyMask': '2', 'FactionMask': '-2'},
            {'ID': '52', 'JournalEncounterID': '30', 'ItemID': '62', 'Flags': '1', 'FactionMask': '-1'},
        ],
        'JournalSectionXDifficulty': [{'ID': '1', 'JournalEncounterSectionID': '42', 'DifficultyID': '2'}],
        'JournalItemXDifficulty': [{'ID': '1', 'JournalEncounterItemID': '51', 'DifficultyID': '2'}],
        'JournalTier': [{'ID': '70', 'Name_lang': '资料片', 'Expansion': '1200'}],
        'JournalTierXInstance': [{'ID': '1', 'JournalInstanceID': '10', 'JournalTierID': '70'}],
        'Map': [{'ID': '20', 'InstanceType': '1'}],
        'MapDifficulty': [{'ID': '1', 'MapID': '20', 'DifficultyID': '1'}, {'ID': '2', 'MapID': '20', 'DifficultyID': '2'}],
        'Difficulty': [{'ID': '1', 'Name_lang': '普通', 'OldEnumValue': '0', 'OrderIndex': '1', 'ItemContext': '1'},
                       {'ID': '2', 'Name_lang': '英雄', 'OldEnumValue': '1', 'OrderIndex': '2', 'ItemContext': '2', 'FallbackDifficultyID': '1'}],
        'Item': [{'ID': '60', 'ClassID': '4', 'InventoryType': '12'}, {'ID': '61', 'ClassID': '4', 'InventoryType': '1'}],
        'ItemSparse': [{'ID': '60', 'Display_lang': '普通饰品', 'InventoryType': '12', 'OverallQualityID': '3', 'AllowableClass': '-1'},
                       {'ID': '61', 'Display_lang': '英雄头盔', 'InventoryType': '1', 'OverallQualityID': '4', 'AllowableClass': '1'}],
    })
    return tables


class JournalCompilationTests(SimpleTestCase):
    def test_unlisted_instance_is_not_published(self):
        tables = fixture()
        tables['JournalInstance'].append({**tables['JournalInstance'][0], 'ID': '999'})
        rows, _, report = compile_journal(tables)
        self.assertEqual([r['id'] for r in rows], [10])
        self.assertEqual(report['excluded_unlisted_instances'], [999])

    def test_world_group_uses_normal_loot_context_without_borrowed_raid_difficulties(self):
        tables = fixture()
        tables['JournalInstance'][0]['Flags'] = '2'
        tables['Difficulty'].append({'ID': '14', 'Name_lang': '普通', 'OrderIndex': '3', 'OldEnumValue': '-1'})
        tables['JournalEncounterItem'][0]['Flags'] = '0'
        rows, _, _ = compile_journal(tables)
        self.assertEqual(rows[0]['kind'], 'world')
        self.assertEqual(rows[0]['difficulty_ids'], [14])
        self.assertEqual(rows[0]['encounters'][0]['loot'][0]['difficulty_ids'], [14])

    def test_hidden_picker_with_per_boss_difficulty_limits_is_not_world_group(self):
        tables = fixture()
        tables['JournalInstance'][0]['Flags'] = '2'
        tables['JournalEncounter'][0].update(Flags='2', DifficultyMask='1')
        rows, _, _ = compile_journal(tables)
        self.assertEqual(rows[0]['kind'], 'dungeon')
        self.assertEqual(rows[0]['difficulty_ids'], [1])

    def test_complete_tree_roles_loot_and_difficulty(self):
        rows, _, report = compile_journal(fixture())
        boss = rows[0]['encounters'][0]
        self.assertEqual(report['sections'], 4)
        self.assertEqual(report['loot'], 2)
        self.assertEqual(boss['sections'][2]['parent'], 41)
        self.assertEqual(boss['sections'][2]['difficulty_ids'], [2])
        self.assertEqual(boss['sections'][0]['roles'], ['tank'])
        self.assertEqual(boss['loot'][0]['difficulty_ids'], [1])
        self.assertEqual(boss['loot'][1]['difficulty_ids'], [2])

    def test_explicit_difficulty_relation_overrides_legacy_mask(self):
        row = {'ID': '1', 'Flags': '2', 'DifficultyMask': '1'}
        self.assertEqual(allowed_difficulties(row, {1: [{'DifficultyID': 2}]}, [1, 2], {}), [2])

    def test_encounter_difficulty_limits_its_skills_and_drops(self):
        tables = fixture()
        tables['JournalEncounter'][0].update(Flags='2', DifficultyMask='1')
        tables['JournalEncounterXDifficulty'] = [{'ID': '1', 'JournalEncounterID': '30', 'DifficultyID': '2'}]
        rows, _, _ = compile_journal(tables)
        boss = rows[0]['encounters'][0]
        self.assertEqual(boss['difficulty_ids'], [2])
        self.assertEqual(boss['sections'][0]['difficulty_ids'], [2])
        self.assertEqual(boss['loot'][0]['difficulty_ids'], [])

    def test_parent_exclusion_propagates_to_child(self):
        tables = fixture()
        tables['JournalEncounterSection'][1]['Flags'] = '2'
        tables['JournalEncounterSection'][1]['DifficultyMask'] = '1'
        rows, _, _ = compile_journal(tables)
        self.assertEqual(rows[0]['encounters'][0]['sections'][2]['difficulty_ids'], [])

    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):
            section_tree([{'ID': 1, 'NextSiblingSectionID': 1}], 1)

    def test_missing_tree_node_rejected(self):
        with self.assertRaises(ValueError):
            section_tree([{'ID': 1, 'FirstChildSectionID': 2}], 1)

    def test_missing_item_report_is_exhaustive(self):
        tables = fixture()
        tables['ItemSparse'] = []
        _, _, report = compile_journal(tables)
        self.assertEqual(report['missing_item_ids'], [60, 61])

    def test_difficulty_specific_inline_text(self):
        self.assertEqual(difficulty_text('基础$[!16史诗$]机制', 1), '基础机制')
        self.assertEqual(difficulty_text('基础$[!16史诗$]机制', 16), '基础史诗机制')
        self.assertEqual(difficulty_text('$[!15英雄尾段', 1), '')

    def test_difficulty_conditions_preserve_bracketed_links_and_nested_blocks(self):
        text = '前$[!16[技能]$[!16强化$]尾$]后$[!15英雄$]'
        self.assertEqual(difficulty_text(text, 16), '前[技能]强化尾后')
        self.assertEqual(difficulty_text(text, 15), '前后英雄')
        self.assertEqual(difficulty_text('$[!2, 8共同机制$]', 8), '共同机制')

    def test_difficulty_choices_preserve_mechanics_and_nested_links(self):
        text = '发射$?DIFF15|DIFF16[两枚[飞弹]][一枚飞弹]。$?DIFF16[至少5人][]'
        self.assertEqual(difficulty_text(text, 16), '发射两枚[飞弹]。至少5人')
        self.assertEqual(difficulty_text(text, 14), '发射一枚飞弹。')
        self.assertEqual(difficulty_text('$?diff16[史诗]?diff15[英雄][普通]', 15), '英雄')

    def test_spell_description_and_journal_addendum_are_both_included(self):
        tables = fixture()
        tables['Spell'] = [{'ID': '100', 'Description_lang': '技能主说明。'}]
        rows, _, _ = compile_journal(tables)
        text = rows[0]['encounters'][0]['sections'][2]['descriptions']['2']
        self.assertEqual(text, '技能主说明。\n\n英雄特有机制')

    def test_spell_name_replaces_internal_section_placeholder_title(self):
        tables = fixture()
        tables['JournalEncounterSection'][2]['Title_lang'] = '第14部分'
        tables['SpellName'] = [{'ID': '100', 'Name_lang': '正式技能名称'}]
        rows, _, _ = compile_journal(tables)
        self.assertEqual(rows[0]['encounters'][0]['sections'][2]['title'], '正式技能名称')

    def test_role_bullets_are_plain_readable_text(self):
        text, unresolved = JournalText(fixture()).resolve('$bullet;打断技能\n$bullet;离开火焰', 0, 1)
        self.assertEqual(text, '• 打断技能\n• 离开火焰')
        self.assertEqual(unresolved, [])

    def test_spell_values_follow_exact_difficulty_and_fallback(self):
        tables = fixture()
        tables['SpellEffect'] = [
            {'ID': 1, 'SpellID': 100, 'EffectIndex': 0, 'DifficultyID': 1, 'EffectBasePointsF': 20},
            {'ID': 2, 'SpellID': 100, 'EffectIndex': 0, 'DifficultyID': 2, 'EffectBasePointsF': 40},
        ]
        resolver = JournalText(tables)
        self.assertEqual(resolver.resolve('造成$s1点伤害', 100, 2), ('造成40点伤害', []))
        self.assertEqual(resolver.resolve('造成$s1点伤害', 100, 1), ('造成20点伤害', []))

    def test_unknown_scaling_never_becomes_zero_damage(self):
        tables = fixture()
        tables['SpellEffect'] = [{'ID': 1, 'SpellID': 100, 'EffectIndex': 0, 'DifficultyID': 0,
                                  'EffectBasePointsF': 0, 'Coefficient': 1.5}]
        text, unresolved = JournalText(tables).resolve('造成$s1点伤害', 100, 2)
        self.assertNotIn('0点', text)
        self.assertEqual(unresolved, ['$s1'])

    def test_fixed_slow_and_radius_are_resolved_without_runtime_damage_scaling(self):
        tables = fixture()
        tables['SpellEffect'] = [
            {'ID': 1, 'SpellID': 100, 'EffectIndex': 0, 'DifficultyID': 0, 'Effect': 2,
             'EffectBasePointsF': 1.5, 'GroupSizeBasePointsCoefficient': 1, 'EffectRadiusIndex_0': 0, 'EffectRadiusIndex_1': 8},
            {'ID': 2, 'SpellID': 100, 'EffectIndex': 1, 'DifficultyID': 0, 'Effect': 6, 'EffectAura': 33,
             'EffectBasePointsF': -40, 'GroupSizeBasePointsCoefficient': 1}]
        tables['SpellRadius'] = [{'ID': 8, 'Radius': 5}]
        resolver = JournalText(tables)
        self.assertEqual(resolver.resolve('范围$A1码，减速$s2%', 100, 2), ('范围5码，减速40%', []))
        self.assertEqual(resolver.resolve('范围$a1码', 100, 2), ('范围5码', []))
        self.assertIn('动态', resolver.resolve('伤害$s1', 100, 2)[0])

    def test_signed_values_are_preserved_inside_expressions(self):
        tables = fixture()
        tables['SpellEffect'] = [{'ID': 1, 'SpellID': 100, 'EffectIndex': 0, 'DifficultyID': 0,
                                  'EffectBasePointsF': -25}]
        resolver = JournalText(tables)
        self.assertEqual(resolver.resolve('降低$s1%，数值${-$s1}', 100, 1), ('降低25%，数值25', []))

    def test_text_links_and_markup_are_not_executable(self):
        text, _ = JournalText(fixture()).resolve('|cFF2959D3|Hspell:100|h[技能]|h|r<script>alert(1)</script>', 0, 1)
        self.assertNotIn('<script>', text)
        self.assertIn('[技能]', text)


class JournalPublicationTests(TestCase):
    def publish(self, tables=None):
        with patch('botend.services.journal_service.WagoJournalSource') as source, \
                patch('botend.services.journal_items.supplement_items', return_value={}):
            source.return_value.load.return_value = tables or fixture()
            source.return_value.manifest = {}
            return sync_journal(build='12.1.0.69587')

    def setUp(self):
        self.release = self.publish()

    def test_published_catalog_and_navigation(self):
        response = self.client.get('/portal/adventure-journal/')
        self.assertContains(response, '测试副本')
        self.assertContains(response, '1 位首领')
        response = self.client.get('/portal/api/navigation/')
        self.assertContains(response, '/portal/adventure-journal/')

    def test_search_matches_boss_name(self):
        response = self.client.get('/portal/api/adventure-journal/', {'q': '测试首领'})
        self.assertEqual(len(response.json()['instances']), 1)
        response = self.client.get('/portal/api/adventure-journal/', {'q': '不存在'})
        self.assertEqual(response.json()['instances'], [])

    def test_catalog_defaults_to_current_season_but_all_is_explicit(self):
        tables = fixture()
        tables['JournalTier'].append({'ID': '505', 'Name_lang': '本赛季', 'Expansion': '9000'})
        tables['JournalInstance'].append({**tables['JournalInstance'][0], 'ID': '11', 'Name_lang': '历史副本'})
        tables['JournalTierXInstance'].extend([
            {'ID': '2', 'JournalInstanceID': '10', 'JournalTierID': '505'},
            {'ID': '3', 'JournalInstanceID': '11', 'JournalTierID': '70'},
        ])
        self.publish(tables)
        current = self.client.get('/portal/api/adventure-journal/').json()
        self.assertEqual(current['tier'], 505)
        self.assertEqual([row['id'] for row in current['instances']], [10])
        all_tiers = self.client.get('/portal/api/adventure-journal/', {'tier': ''}).json()
        self.assertEqual(len(all_tiers['instances']), 2)

    def test_switch_boss_and_difficulty_keep_loot_filters(self):
        from urllib.parse import parse_qs, urlsplit
        response = self.client.get('/portal/adventure-journal/10/', {
            'difficulty': 2, 'role': 'healer', 'class': 1, 'slot': 1,
            'faction': 'alliance', 'loot_q': '英雄', 'tab': 'loot',
        })
        params = parse_qs(urlsplit(response.context['bosses'][0]['url']).query)
        self.assertEqual(params['tab'], ['skills'])
        self.assertEqual(params['slot'], ['1'])
        self.assertEqual(params['class'], ['1'])
        self.assertEqual(params['loot_q'], ['英雄'])
        self.assertContains(response, 'name="slot" value="1"')
        self.assertContains(response, 'name="class" value="1"')
        self.assertContains(response, 'data-auto-filter')

    def test_equipment_details_are_inline_when_cached(self):
        details = {'complete': True, 'item_level': 289, 'stats': ['+118 敏捷／智力'],
                   'effects': ['使用：急速提高800，持续15秒。']}
        with patch('botend.portal.adventure_journal.cached_tooltip', return_value=details):
            response = self.client.get('/portal/adventure-journal/10/', {'tab': 'loot'})
        self.assertContains(response, 'role="table"')
        self.assertContains(response, '装等 289')
        self.assertContains(response, '使用：急速提高800，持续15秒。')
        self.assertNotContains(response, '正在加载特效')

    def test_difficulty_role_and_loot_filters_do_not_leak(self):
        url = '/portal/api/adventure-journal/10/'
        normal = self.client.get(url, {'difficulty': 1, 'role': 'tank'}).json()['boss']
        self.assertEqual([r['name'] for r in normal['loot']], ['普通饰品'])
        self.assertEqual([r['title'] for r in normal['roles']], ['坦克'])
        self.assertEqual(normal['skills'][0]['children'], [])
        heroic = self.client.get(url, {'difficulty': 2, 'role': 'healer', 'class': 1}).json()['boss']
        self.assertEqual(heroic['loot'][0]['name'], '英雄头盔')
        self.assertEqual(heroic['skills'][0]['children'][0]['title'], '英雄技能')
        self.assertEqual(len(self.client.get(url, {'difficulty': 2, 'faction': 'horde'}).json()['boss']['loot']), 1)
        self.assertEqual(self.client.get(url, {'difficulty': 2, 'class': 8}).json()['boss']['loot'], [])

    def test_slot_search_and_hostile_class_id(self):
        url = '/portal/api/adventure-journal/10/'
        self.assertEqual(self.client.get(url, {'slot': 1}).json()['boss']['loot'], [])
        self.assertEqual(len(self.client.get(url, {'loot_q': '60'}).json()['boss']['loot']), 1)
        self.assertEqual(self.client.get(url, {'class': '999999999999'}).status_code, 200)

    def test_detail_template_renders_loot_tab(self):
        response = self.client.get('/portal/adventure-journal/10/', {'difficulty': 2, 'tab': 'loot'})
        self.assertContains(response, '英雄头盔')
        self.assertContains(response, '副本掉落')
        self.assertNotContains(response, 'role="tab"')
        self.assertNotContains(response, 'name="faction"')
        self.assertNotContains(response, '无额外装备特效')

    def test_instance_loot_aggregates_sources_and_filters_type_independently_of_guide(self):
        tables = fixture()
        tables['JournalEncounter'].append({**tables['JournalEncounter'][0], 'ID': '31', 'Name_lang': '第二首领', 'FirstSectionID': '0'})
        tables['Item'].append({'ID': '63', 'ClassID': '4', 'SubclassID': '4', 'InventoryType': '5'})
        tables['ItemSparse'].append({'ID': '63', 'Display_lang': '板甲胸甲', 'InventoryType': '5', 'AllowableClass': '-1'})
        tables['JournalEncounterItem'].extend([
            {'ID': '53', 'JournalEncounterID': '31', 'ItemID': '63', 'Flags': '0', 'FactionMask': '-1'},
            {'ID': '54', 'JournalEncounterID': '31', 'ItemID': '60', 'Flags': '0', 'FactionMask': '-1'},
        ])
        self.publish(tables)
        url = '/portal/api/adventure-journal/10/'
        data = self.client.get(url).json()
        self.assertEqual(data['tab'], 'loot')
        self.assertEqual(data['loot_total'], 2)
        self.assertEqual({r['item_id'] for r in data['loot']}, {60, 63})
        shared = next(r for r in data['loot'] if r['item_id'] == 60)
        self.assertEqual([s['id'] for s in shared['sources']], [30, 31])
        data = self.client.get(url, {'boss': 30, 'tab': 'loot', 'item_type': 'armor:4'}).json()
        self.assertEqual([r['item_id'] for r in data['loot']], [63])
        self.assertEqual(data['loot'][0]['type_name'], '板甲')
        first = self.client.get(url, {'loot_boss': 30}).json()
        self.assertEqual([r['item_id'] for r in first['loot']], [60])
        second = self.client.get(url, {'loot_boss': 31}).json()
        self.assertEqual({r['item_id'] for r in second['loot']}, {60, 63})
        with patch('botend.services.journal_tooltip.tooltip', return_value={'name': '板甲胸甲', 'lines': []}):
            self.assertEqual(self.client.get('/portal/api/adventure-journal/10/tooltip/item/63/').status_code, 200)
        guide = self.client.get('/portal/adventure-journal/10/', {'boss': 31, 'tab': 'skills'})
        self.assertContains(guide, '战斗指南')
        self.assertNotContains(guide, 'class="journal-loot-table"')

    def test_equipment_type_distinguishes_material_weapon_and_accessory(self):
        from botend.services.journal_loot import equipment_type
        self.assertEqual(equipment_type({'class_id': 4, 'subclass_id': 1, 'slot': 5}), ('armor:1', '布甲'))
        self.assertEqual(equipment_type({'class_id': 2, 'subclass_id': 10, 'slot': 17}), ('weapon:10', '法杖'))
        self.assertEqual(equipment_type({'class_id': 4, 'subclass_id': 0, 'slot': 12}), ('slot:12', '饰品'))

    def test_invalid_instance_boss_or_difficulty_is_404(self):
        for url in ['/portal/adventure-journal/999/', '/portal/adventure-journal/10/?boss=999',
                    '/portal/adventure-journal/10/?difficulty=999']:
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_switch_difficulty_selects_the_matching_variant_and_filters_boss_list(self):
        tables = fixture()
        tables['JournalEncounter'][0].update(Flags='2', DifficultyMask='1')
        tables['JournalEncounter'].append({**tables['JournalEncounter'][0], 'ID': '31', 'DifficultyMask': '2', 'FirstSectionID': '0'})
        self.publish(tables)
        data = self.client.get('/portal/api/adventure-journal/10/', {'boss': 30, 'difficulty': 2}).json()
        self.assertEqual(data['boss']['id'], 31)
        self.assertEqual([b['id'] for b in data['bosses']], [31])

    def test_failed_fetch_preserves_active_release_and_unlocks(self):
        with patch('botend.services.journal_service.WagoJournalSource') as source:
            source.return_value.load.side_effect = ValueError('模拟下载截断')
            with self.assertRaises(ValueError):
                sync_journal(build='12.1.0.69587')
        state = JournalState.objects.get(pk='wow-zhCN')
        self.assertEqual(state.active_release_id, self.release.id)
        self.assertEqual(state.sync_token, '')
        self.assertEqual(JournalRelease.objects.first().status, 'failed')

    def test_missing_item_prevents_publication(self):
        tables = fixture()
        tables['ItemSparse'] = []
        with self.assertRaises(ValueError):
            self.publish(tables)
        self.assertEqual(JournalState.objects.get(pk='wow-zhCN').active_release_id, self.release.id)

    def test_repeat_sync_replaces_snapshot_without_duplicate_bosses(self):
        second = self.publish()
        self.assertNotEqual(second.id, self.release.id)
        self.assertEqual(JournalInstance.objects.filter(release=second).count(), 1)
        self.assertEqual(len(self.client.get('/portal/api/adventure-journal/10/').json()['bosses']), 1)

    def test_full_sync_preserves_legacy_ptr_overlay_catalog(self):
        ptr_catalog = {
            'art_ids': [7955915],
            'tiers': [{'id': 516, 'name': '至暗之夜', 'order': 1200}],
            'difficulties': [{'id': 14, 'name': '普通', 'context': 3}],
        }
        manifest = deepcopy(self.release.manifest)
        manifest['ptr_overlays'] = {
            '1324': {
                'source_build': '12.1.5.69594',
                'artifact_sha256': 'ptr-artifact',
            },
        }
        manifest['catalog']['art_ids'].extend(ptr_catalog['art_ids'])
        manifest['catalog']['tiers'].extend(ptr_catalog['tiers'])
        manifest['catalog']['difficulties'].extend(ptr_catalog['difficulties'])
        self.release.manifest = manifest
        self.release.report = {**self.release.report, 'instances': 2, 'encounters': 2, 'ptr_overlays': [1324]}
        self.release.build = '12.1.0.69587+ptr-12.1.5.69594'
        self.release.save()
        instance = JournalInstance.objects.create(
            release=self.release,
            journal_id=1324,
            name="Kith'ix Unbound",
            kind='raid',
            expansion=1200,
            payload={'id': 1324, 'name': "Kith'ix Unbound", 'kind': 'raid', 'expansion': 1200,
                     'tier_ids': [516], 'difficulty_ids': [14], 'image': 7955915},
        )
        JournalEncounter.objects.create(
            instance=instance,
            journal_id=2700,
            name='Kithix',
            order=1,
            payload={'id': 2700, 'name': 'Kithix', 'order': 1, 'difficulty_ids': [14],
                     'sections': [], 'loot': []},
        )

        second = self.publish()

        self.assertEqual(
            set(JournalInstance.objects.filter(release=second).values_list('journal_id', flat=True)),
            {10, 1324},
        )
        self.assertEqual(second.manifest['ptr_overlays']['1324']['artifact_sha256'], 'ptr-artifact')
        self.assertIn(7955915, second.manifest['catalog']['art_ids'])
        self.assertEqual(second.report['ptr_overlays'], [1324])
        self.assertEqual(second.build, '12.1.0.69587+ptr-12.1.5.69594')

    def test_full_sync_preserves_release_published_after_fetch_started(self):
        concurrent = JournalRelease.objects.create(
            build='12.1.0.69587+ptr-12.1.5.69594',
            status='completed',
            manifest={'catalog': self.release.manifest['catalog'], 'ptr_overlays': {}},
            report=self.release.report,
            completed_at=timezone.now(),
        )
        real_compile = compile_journal

        def compile_after_concurrent_publish(*args, **kwargs):
            result = real_compile(*args, **kwargs)
            JournalState.objects.filter(pk='wow-zhCN').update(
                active_release=concurrent,
                sync_until=timezone.now() - timedelta(seconds=1),
            )
            return result

        captured = []

        def capture_active_release(active_release, rows, catalog, report, retail_build):
            captured.append(active_release.pk)
            return rows, catalog, report, {}, retail_build

        with patch('botend.services.journal_service.compile_journal', side_effect=compile_after_concurrent_publish), \
                patch('botend.services.ptr_journal_gear_overlay.preserve_active_ptr_journal_overlay',
                      side_effect=capture_active_release):
            self.publish()

        self.assertEqual(captured, [concurrent.pk])

    def test_retail_instance_replaces_overlay_with_same_id(self):
        manifest = deepcopy(self.release.manifest)
        manifest['ptr_overlays'] = {
            '10': {
                'source_build': '12.1.5.69594',
                'artifact_sha256': 'old-ptr-artifact',
                'catalog': {},
            },
        }
        self.release.manifest = manifest
        self.release.build = '12.1.0.69587+ptr-12.1.5.69594'
        self.release.save(update_fields=['manifest', 'build'])

        second = self.publish()

        self.assertEqual(second.build, '12.1.0.69587')
        self.assertEqual(second.manifest['ptr_overlays'], {})
        self.assertEqual(second.report.get('ptr_overlays') or [], [])
        self.assertEqual(JournalInstance.objects.get(release=second, journal_id=10).name, '测试副本')

    def test_parallel_sync_is_rejected(self):
        JournalState.objects.filter(pk='wow-zhCN').update(sync_token='其他运行', sync_until=timezone.now() + timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, '已有同步'):
            self.publish()

    def test_cross_snapshot_art_is_not_an_open_proxy(self):
        self.assertEqual(self.client.get('/portal/adventure-journal/art/999999/').status_code, 404)

    def test_tooltip_only_fetches_referenced_items_in_selected_difficulty(self):
        with patch('botend.services.journal_tooltip.tooltip', return_value={'name': '普通饰品', 'lines': ['属性']}) as fetch:
            url = '/portal/api/adventure-journal/10/tooltip/item/60/'
            self.assertEqual(self.client.get(url, {'difficulty': 1}).json()['name'], '普通饰品')
            self.assertEqual(self.client.get(url, {'difficulty': 2}).status_code, 404)
            self.assertEqual(fetch.call_count, 1)

    def test_tooltip_failure_is_recoverable(self):
        with patch('botend.services.journal_tooltip.tooltip', side_effect=ValueError('来源失败')):
            response = self.client.get('/portal/api/adventure-journal/10/tooltip/item/60/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['source'], 'Wago')
            self.assertEqual(response.json()['name'], '普通饰品')
            self.assertIn('暂不可访问', response.json()['note'])

    def test_spell_tooltip_uses_the_selected_published_description(self):
        response = self.client.get('/portal/api/adventure-journal/10/tooltip/spell/100/', {'difficulty': 2})
        self.assertEqual(response.json()['source'], 'Wago')
        self.assertEqual(response.json()['lines'], ['英雄特有机制'])

    def test_monitor_is_appended_and_checks_weekly_without_resyncing_same_build(self):
        from LMonitor.config import Monitor_Type_BaseObject_List
        from botend.plugin_sync import monitor_default_wait_time
        from botend.controller.plugins.wow.AdventureJournalMonitor import AdventureJournalMonitor
        self.assertEqual(Monitor_Type_BaseObject_List[34].__name__, 'MaxrollClassGuideMonitor')
        self.assertEqual(Monitor_Type_BaseObject_List[35].__name__, 'AdventureJournalMonitor')
        self.assertEqual(monitor_default_wait_time('AdventureJournalMonitor'), 604800)
        task = SimpleNamespace(flag='')
        monitor = AdventureJournalMonitor.__new__(AdventureJournalMonitor)
        monitor.task = task
        monitor.last_error_detail = ''
        with patch(
            'botend.controller.plugins.wow.AdventureJournalMonitor.latest_retail_build',
            return_value='12.1.0.69587',
        ), patch(
            'botend.controller.plugins.wow.AdventureJournalMonitor.current_release_build',
            return_value='12.1.0.69587+ptr-12.1.5.69594',
        ), patch(
            'botend.controller.plugins.wow.AdventureJournalMonitor.sync_journal',
        ) as sync:
            self.assertTrue(monitor.scan(''))
        sync.assert_not_called()
        self.assertIn('无需完整同步', task.flag)


class JournalSourceTests(SimpleTestCase):
    def test_class_filter_checks_armor_weapon_and_primary_stats(self):
        from botend.services.journal_loot import class_matches
        cloth = {'class_id': 4, 'subclass_id': 1, 'slot': 1, 'class_mask': -1, 'stat_types': [5]}
        self.assertTrue(class_matches(cloth, 8))
        self.assertFalse(class_matches(cloth, 1))
        sword = {'class_id': 2, 'subclass_id': 8, 'slot': 17, 'class_mask': -1, 'stat_types': [4]}
        self.assertTrue(class_matches(sword, 1))
        self.assertFalse(class_matches(sword, 8))
        trinket = {'class_id': 4, 'subclass_id': 0, 'slot': 12, 'class_mask': -1, 'stat_types': [73]}
        self.assertTrue(class_matches(trinket, 8))
        self.assertTrue(class_matches(trinket, 4))
        self.assertFalse(class_matches(trinket, 1))
        self.assertTrue(class_matches(trinket, 0))

    def test_inline_tooltip_separates_static_stats_and_full_effect_values(self):
        from botend.services.journal_tooltip import tooltip
        payload = {'name': '烬翼羽毛', 'icon': 'inv_feather', 'quality': 4, 'tooltip': (
            '<b>烬翼羽毛</b><br>物品等级：<!--ilvl-->289<br><!--rf-->'
            '+118 [敏捷 or 智力]<br><!--nameDescStats-->'
            '<!--useText:1:1-->使用: 急速提高800，持续15秒。<br>其他属性降低249，持续10秒。<!--useText:1:1-->'
        )}
        with patch('botend.services.journal_tooltip.cached_tooltip', return_value=None), \
                patch('botend.services.journal_tooltip.cache') as cache, \
                patch('botend.services.journal_tooltip.requests.Session') as session, \
                patch('botend.services.journal_tooltip.cache_path'):
            cache.get.return_value = None
            client = session.return_value.__enter__.return_value
            client.get.return_value.json.return_value = payload
            result = tooltip('item', 250144, 1, '12.1.0.69587')
        self.assertEqual(result['item_level'], 289)
        self.assertEqual(result['stats'], ['+118 敏捷／智力'])
        self.assertIn('800', result['effects'][0])
        self.assertIn('249', result['effects'][0])
        self.assertNotIn('dd', client.get.call_args.kwargs['params'])
        self.assertIn('参考装等', result['note'])

    def test_localized_names_do_not_overwrite_current_item_properties(self):
        tables = fixture()
        tables['ItemSparse'] = []
        english = [{'ID': '60', 'Display_lang': 'Trinket', 'OverallQualityID': '3'},
                   {'ID': '61', 'Display_lang': 'Helm', 'OverallQualityID': '4'}]
        source = SimpleNamespace(directory=Path('未使用的缓存') / '版本' / 'zhCN', offline=True, refresh=False,
                                 progress=lambda _: None, table=Mock(return_value=english), manifest={})
        with patch('pathlib.Path.exists', return_value=False), patch('botend.services.journal_items.WagoJournalSource') as legacy:
            legacy.return_value.table.return_value = [{'ID': '60', 'Display_lang': '历史中文名称', 'OverallQualityID': '5'}]
            legacy.return_value.manifest = {'ItemSparse': {}}
            supplements = supplement_items(tables, source)
        self.assertEqual(supplements[60]['name'], '历史中文名称')
        self.assertEqual(supplements[61]['source'], 'wago-enUS')
        rows, _, report = compile_journal(tables, item_fallback=supplements)
        self.assertEqual(rows[0]['encounters'][0]['loot'][0]['quality'], 3)
        self.assertEqual(report['unlocalized_item_ids'], [61])
        self.assertEqual(report['missing_item_ids'], [])

    def test_retired_item_name_does_not_invent_quality(self):
        tables = fixture()
        tables['ItemSparse'] = []
        rows, _, _ = compile_journal(tables, item_fallback={60: {'name': '历史兑换物'}, 61: {'name': '历史兑换物'}})
        self.assertIsNone(rows[0]['encounters'][0]['loot'][0]['quality'])

    def segmented_response(self, lines):
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {'total': 2, 'per_page': 25, 'data': [{'ID': 10}]}
        response.iter_lines.return_value = [line.encode('utf-8') for line in lines]
        return response

    def test_segmented_csv_preserves_quoted_multiline_fields_and_id_boundary(self):
        response = self.segmented_response(['ID,Display_lang', '10,"跨行', '名称"', '20,第二件'])
        with patch('botend.services.journal_source.http_session') as session, \
                patch('pathlib.Path.exists', return_value=False), patch('pathlib.Path.mkdir'), \
                patch('pathlib.Path.write_text'):
            client = session.return_value.__enter__.return_value
            client.get.return_value = response
            content = WagoJournalSource('12.1.0.69587', '.').segmented_table('ItemSparse', 'zhCN')
            self.assertIn('跨行\n名称', content.decode('utf-8'))
            self.assertEqual(client.get.call_args.kwargs['params']['filter[ID]'], '>9')

    def test_segmented_csv_rejects_short_or_wrong_boundary_response(self):
        for lines in (['ID,Display_lang', '10,只有一件'], ['ID,Display_lang', '11,错误起点', '20,第二件']):
            response = self.segmented_response(lines)
            with patch('botend.services.journal_source.http_session') as session, \
                    patch('pathlib.Path.exists', return_value=False), patch('pathlib.Path.mkdir'), \
                    patch('pathlib.Path.write_text') as write:
                session.return_value.__enter__.return_value.get.return_value = response
                with self.assertRaisesRegex(ValueError, '不完整数据'):
                    WagoJournalSource('12.1.0.69587', '.').segmented_table('ItemSparse', 'zhCN')
                write.assert_not_called()

    def test_explicit_build_required(self):
        with self.assertRaises(ValueError):
            WagoJournalSource('../../', '.')

    def test_offline_empty_and_duplicate_tables_rejected(self):
        with patch('pathlib.Path.exists', return_value=True), patch('pathlib.Path.read_bytes') as read:
            source = WagoJournalSource('12.1.0.69587', '未使用的离线路径', offline=True)
            for content in ('ID,Name_lang\n', 'ID,Name_lang\n1,一\n1,二\n'):
                read.return_value = content.encode('utf-8')
                with self.assertRaises(ValueError):
                    source.table('JournalInstance')

    @patch('botend.services.journal_source.http_session')
    @patch('botend.services.journal_source.inertia')
    def test_retail_selection_excludes_ptr(self, props, session):
        props.return_value = {'builds': {'data': [{'product': 'wowxptr', 'version': '12.9.0.99999'},
                                                  {'product': 'wow', 'version': '12.1.0.69587'}]}}
        self.assertEqual(latest_retail_build(), '12.1.0.69587')
