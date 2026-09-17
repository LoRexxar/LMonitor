from pathlib import Path

from django.test import TestCase, override_settings

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import serialize_variant
from botend.services.simc_benchmark_config import _benchmark_item_display_metadata
from botend.services.simc_player_config import parse_manual_player_config
from botend.services.simc_result_analysis import parse_simc_html_report
from botend.services.wow_item_display import item_display_metadata, load_item_tooltip_metadata


ROOT = Path(__file__).resolve().parents[2]


class SimcEquipmentTooltipContractTests(TestCase):
    def test_equipment_tooltip_preserves_source_stat_positions_and_item_specific_order(self):
        item = WowItemSnapshot.objects.create(
            item_id=239051, name_zh='一统肩甲', catalog_type='equipment', slot_key='shoulders',
            description_zh='\n'.join([
                '史诗钥石',
                '升级：神话 6/6',
                '肩部 板甲',
                '299护甲',
                '+141 [力量 or 智力]',
                '+2,932 耐力',
                '+ 85全能',
                '+ 66精通',
                '耐久: 85 / 85 需要等级 90',
                '掉落于: 达萨大王',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=334,
            stats={
                'stamina': 2932,
                'armor': 299,
                'mastery': 66,
                'versatility': 85,
                'intellect': 141,
            },
            sources=[{'type': 'mythic_plus', 'instance_zh': '诸王之眠'}],
        )

        self.assertEqual(display['stat_lines'], [
            '+299 护甲',
            '+141 智力',
            '+2,932 耐力',
            '+85 全能',
            '+66 精通',
        ])
        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 334',
            '史诗钥石',
            '升级：神话 6/6',
            '肩部 板甲',
            '+299 护甲',
            '+141 智力',
            '+2,932 耐力',
            '+85 全能',
            '+66 精通',
            '耐久: 85 / 85 需要等级 90',
            '掉落于: 达萨大王',
            '来源：大秘境 · 诸王之眠',
        ])

    def test_equipment_tooltip_preserves_effect_position_from_source_description(self):
        item = WowItemSnapshot.objects.create(
            item_id=270160, name_zh='大副的甲壳结界', catalog_type='equipment', slot_key='trinket',
            description_zh='\n'.join([
                '史诗钥石',
                '饰品',
                '+159 力量',
                '装备：旧装等效果。',
                '需要等级 90',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            stats={'strength': 159},
            effects=[{'description_zh': '装备：321 装等效果。'}],
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '史诗钥石',
            '饰品',
            '+159 力量',
            '装备：321 装等效果。',
            '需要等级 90',
        ])

    def test_equipment_tooltip_drops_stale_or_duplicate_source_stats_when_structured_stats_exist(self):
        item = WowItemSnapshot.objects.create(
            item_id=270161, name_zh='测试胸甲', catalog_type='equipment', slot_key='chest',
            description_zh='\n'.join([
                '胸部 板甲',
                '100 护甲',
                '+50 暴击',
                '+50 暴击',
                '需要等级 90',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            stats={'armor': 120},
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '胸部 板甲',
            '+120 护甲',
            '需要等级 90',
        ])

    def test_equipment_tooltip_replaces_multiple_effects_at_their_source_positions(self):
        item = WowItemSnapshot.objects.create(
            item_id=270162, name_zh='测试饰品', catalog_type='equipment', slot_key='trinket',
            description_zh='\n'.join([
                '饰品',
                '装备：旧效果 A，造成 100 点伤害。',
                '生命值较低时额外吸收 200 点伤害。',
                '装备唯一：测试分组（1）',
                '使用：旧效果 B。',
                '需要等级 90',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            effects=[
                {'description_zh': '装备：新效果 A，造成 110 点伤害。\n生命值较低时额外吸收 220 点伤害。'},
                {'description_zh': '使用：新效果 B。'},
            ],
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '饰品',
            '装备：新效果 A，造成 110 点伤害。',
            '生命值较低时额外吸收 220 点伤害。',
            '装备唯一：测试分组（1）',
            '使用：新效果 B。',
            '需要等级 90',
        ])

    def test_equipment_tooltip_consumes_source_effect_wrapped_inside_current_line(self):
        item = WowItemSnapshot.objects.create(
            item_id=246305, name_zh='远行者的暗月徽记', catalog_type='equipment', slot_key='trinket',
            description_zh='\n'.join([
                '饰品',
                '装备：你的伤害法术和技能有几率赋予远行者的机敏',
                '，使你最低的次要属性提高 379，持续15秒。',
                '当一名友方玩家死亡时，你最高的次要属性提高 232，持续15秒。',
                '此效果会被暗月徽记：鲜血所强化。',
                '需要等级 90',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=305,
            effects=[{
                'description_zh': '\n'.join([
                    '装备：你的伤害法术和技能有几率赋予远行者的机敏，使你最低的次要属性提高656，持续15秒。',
                    '当一名友方玩家死亡时，你最高的次要属性提高561，持续15秒。',
                    '此效果会被暗月徽记：鲜血所强化。',
                ]),
            }],
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 305',
            '饰品',
            '装备：你的伤害法术和技能有几率赋予远行者的机敏，使你最低的次要属性提高656，持续15秒。',
            '当一名友方玩家死亡时，你最高的次要属性提高561，持续15秒。',
            '此效果会被暗月徽记：鲜血所强化。',
            '需要等级 90',
        ])

    def test_authoritative_effect_drops_stale_stats_and_unmatched_source_continuation(self):
        item = WowItemSnapshot.objects.create(
            item_id=270164, name_zh='换行错位测试饰品', catalog_type='equipment', slot_key='trinket',
            description_zh='\n'.join([
                '饰品',
                '+50 暴击',
                '装备：旧装等效果。',
                '旧装等完全不同措辞的续行 232。',
                '需要等级 90',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            stats={},
            effects=[{'description_zh': '装备：当前装等效果。'}],
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '饰品',
            '装备：当前装等效果。',
            '需要等级 90',
        ])

    def test_authoritative_effect_preserves_quoted_flavor_text_after_source_effect(self):
        item = WowItemSnapshot.objects.create(
            item_id=270165, name_zh='风味文本测试饰品', catalog_type='equipment', slot_key='trinket',
            description_zh='\n'.join([
                '饰品',
                '装备：旧装等效果。',
                '“这是一句应保留的风味文本。”',
                '需要等级 90',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            effects=[{'description_zh': '装备：当前装等效果。'}],
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '饰品',
            '装备：当前装等效果。',
            '“这是一句应保留的风味文本。”',
            '需要等级 90',
        ])

    def test_snapshot_without_structured_projection_uses_cleaned_fallback(self):
        item = WowItemSnapshot.objects.create(
            item_id=270166,
            name_zh='清理测试物品',
            catalog_type='equipment',
            description_zh='清理测试物品\n最大叠加: 20\n普通说明',
        )

        display = item_display_metadata(item.item_id, item)

        self.assertEqual(display['tooltip'], '普通说明')

    def test_explicit_empty_effects_do_not_recover_stale_source_effect(self):
        item = WowItemSnapshot.objects.create(
            item_id=270167,
            name_zh='空特效权威投影',
            catalog_type='equipment',
            slot_key='trinket',
            description_zh='饰品\n装备：旧装等效果。\n需要等级 90',
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            effects=[],
        )

        self.assertEqual(display['effects'], [])
        self.assertEqual(display['tooltip'], '物品等级 321\n饰品\n需要等级 90')
        self.assertFalse(display['tooltip_complete'])

    def test_stats_only_projection_does_not_recover_stale_source_effect(self):
        item = WowItemSnapshot.objects.create(
            item_id=270168,
            name_zh='纯属性权威投影',
            catalog_type='equipment',
            slot_key='chest',
            description_zh='胸部 板甲\n+100 力量\n装备：旧装等效果。\n需要等级 90',
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            stats={'strength': 120},
        )

        self.assertEqual(display['effects'], [])
        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '胸部 板甲',
            '+120 力量',
            '需要等级 90',
        ])

    def test_structured_projection_uses_cleaned_layout_without_name_or_stack_noise(self):
        item = WowItemSnapshot.objects.create(
            item_id=270169,
            name_zh='结构化清理测试胸甲',
            catalog_type='equipment',
            slot_key='chest',
            description_zh='\n'.join([
                '结构化清理测试胸甲',
                '最大叠加: 20',
                '胸部 板甲',
                '+100 力量',
                '普通说明',
            ]),
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            stats={'strength': 120},
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '胸部 板甲',
            '+120 力量',
            '普通说明',
        ])

    def test_blank_line_ends_source_effect_block_before_unquoted_flavor_text(self):
        item = WowItemSnapshot.objects.create(
            item_id=270170,
            name_zh='段落风味测试饰品',
            catalog_type='equipment',
            slot_key='trinket',
            description_zh='饰品\n装备：旧装等效果。\n旧效果续行。\n\n这是一句无引号风味文本。\n需要等级 90',
        )

        display = item_display_metadata(
            item.item_id,
            item,
            item_level=321,
            effects=[{'description_zh': '装备：当前装等效果。'}],
        )

        self.assertEqual(display['tooltip'].splitlines(), [
            '物品等级 321',
            '饰品',
            '装备：当前装等效果。',
            '这是一句无引号风味文本。',
            '需要等级 90',
        ])

    def test_exact_build_request_selects_variant_from_shared_catalog(self):
        season = SeasonMeta.objects.create(
            season_key='tooltip-build', season_name='构建隔离测试', is_active=True,
            game_build='12.1.0.1', gear_batch_key='tooltip-build-batch', gear_sync_status='ready',
            mplus_zone_id=1, raid_zone_id=2,
        )
        item = WowItemSnapshot.objects.create(
            item_id=281235, name_zh='共享目录装备', catalog_type='equipment', slot_key='chest',
        )
        for build, level, intellect in (
            ('12.1.0.1', 289, 100),
            ('12.1.5.2', 292, 128),
        ):
            WowItemVariantSnapshot.objects.create(
                item=item, season=season, batch_key=season.gear_batch_key,
                game_build=build, variant_key=f'{build}-{level}',
                variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                item_level=level, stats_json={'intellect': intellect}, effects_json=[],
            )

        live, ptr = load_item_tooltip_metadata([
            {'item_id': item.item_id, 'game_build': '12.1.0.1', 'allow_default_variant': True},
            {'item_id': item.item_id, 'game_build': '12.1.5.2', 'allow_default_variant': True},
        ])

        self.assertEqual(live['item_level'], 289)
        self.assertEqual(live['stat_lines'], ['+100 智力'])
        self.assertEqual(ptr['item_level'], 292)
        self.assertEqual(ptr['stat_lines'], ['+128 智力'])

    def test_dynamic_primary_stat_is_resolved_for_requested_spec(self):
        season = SeasonMeta.objects.create(
            season_key='tooltip-primary', season_name='动态主属性测试', is_active=True,
            game_build='12.1.0.1', gear_batch_key='tooltip-primary-batch', gear_sync_status='ready',
            mplus_zone_id=1, raid_zone_id=2,
        )
        item = WowItemSnapshot.objects.create(
            item_id=270173, name_zh='祖尔金的处斩技法', icon='inv_dynamic_primary',
            catalog_type='equipment', slot_key='trinket', inventory_type=12,
        )
        WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key=season.gear_batch_key,
            variant_key='benchmark-321', variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=321, compatible_slots=['trinket'], stats_json={},
            effects_json=[{'description_zh': '装备：触发处斩。'}],
            metadata={'primary_stat_values': {'agility': 159, 'strength': 159}},
        )

        strength, agility = load_item_tooltip_metadata([
            {'item_id': 270173, 'item_level': 321, 'spec_key': 'deathknight_blood'},
            {'item_id': 270173, 'item_level': 321, 'spec_key': 'rogue_outlaw'},
        ])

        self.assertEqual(strength['stats'], {'strength': 159})
        self.assertIn('+159 力量', strength['tooltip'])
        self.assertEqual(agility['stats'], {'agility': 159})
        self.assertIn('+159 敏捷', agility['tooltip'])

    def test_fixed_primary_stat_is_preserved_for_other_specs(self):
        season = SeasonMeta.objects.create(
            season_key='tooltip-fixed-primary', season_name='固定主属性测试', is_active=True,
            game_build='12.1.0.1', gear_batch_key='tooltip-fixed-primary-batch',
            gear_sync_status='ready', mplus_zone_id=1, raid_zone_id=2,
        )
        item = WowItemSnapshot.objects.create(
            item_id=264878, name_zh='阿斯塔洛的苦痛煽动者',
            catalog_type='equipment', slot_key='trinket', inventory_type=12,
        )
        WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key=season.gear_batch_key,
            variant_key='benchmark-321', variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=321, compatible_slots=['trinket'], stats_json={'intellect': 159},
            effects_json=[{'description_zh': '使用: 发射苦痛箭矢。'}],
        )

        display = load_item_tooltip_metadata([
            {'item_id': 264878, 'item_level': 321, 'spec_key': 'deathknight_blood'},
        ])[0]

        self.assertEqual(display['stats'], {'intellect': 159})
        self.assertIn('+159 智力', display['tooltip'])

    def test_three_equipment_entries_match_the_same_item_level_variant(self):
        season = SeasonMeta.objects.create(
            season_key='tooltip-unified', season_name='Tooltip 统一测试', is_active=True,
            game_build='12.1.0.1', gear_batch_key='tooltip-batch', gear_sync_status='ready',
            mplus_zone_id=1, raid_zone_id=2,
        )
        item = WowItemSnapshot.objects.create(
            item_id=270160, name='First Mate Shield', name_zh='大副的甲壳结界',
            description='Equip: stale item-level 334 text.', icon='inv_unified_tooltip',
            catalog_type='equipment', slot_key='trinket', inventory_type=12,
        )
        level_321 = WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key='tooltip-batch', game_build='12.1.0.1',
            variant_key='hero-6', variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=321, compatible_slots=['trinket'], stats_json={'strength': 159},
            effects_json=[{'description_zh': '装备：321 装等效果。'}],
            source_json=[{'type': 'raid', 'instance_zh': '测试团本'}],
        )
        WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key='tooltip-batch', game_build='12.1.0.1',
            variant_key='myth-6', variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=334, compatible_slots=['trinket'], stats_json={'strength': 179},
            effects_json=[{'description_zh': '装备：334 装等效果。'}],
        )

        builder = serialize_variant(level_321, 'DeathKnight', 'Blood')
        profile = parse_manual_player_config(
            'deathknight="Tooltip"\nlevel=90\nspec=blood\ntrinket1=,id=270160,ilevel=321',
            'deathknight_blood',
        )['equipment'][0]
        _label, benchmark_tooltip, _icon = _benchmark_item_display_metadata(270160, 321)

        self.assertEqual(builder['tooltip'], profile['tooltip'])
        self.assertEqual(profile['tooltip'], benchmark_tooltip)
        self.assertIn('+159 力量', benchmark_tooltip)
        self.assertIn('装备：321 装等效果。', benchmark_tooltip)
        self.assertNotIn('334', benchmark_tooltip)

    @override_settings(OSS_CONFIG={
        'base_url': 'https://oss.wowdaily.cn/',
        'wow_icon_prefix': 'wow_icons_oss',
    })
    def test_profile_result_and_benchmark_equipment_share_item_description_tooltips(self):
        WowItemSnapshot.objects.create(
            item_id=249952,
            name='Night Ender\'s Tusks',
            name_zh='夜幕终结者的獠牙',
            description='Equip: English effect.',
            description_zh='装备：中文装备属性与特效。',
            icon='inv_test_equipment_icon',
        )
        profile = parse_manual_player_config(
            '\n'.join([
                'warrior="TooltipTest"',
                'level=90',
                'spec=fury',
                '# Night Ender\'s Tusks (289)',
                'head=,id=249952,ilevel=289',
            ]),
            'warrior_fury',
        )
        self.assertEqual(profile['equipment'][0]['display_description'], '物品等级 289\n装备：中文装备属性与特效。')
        expected_icon_url = 'https://oss.wowdaily.cn/wow_icons_oss/small/inv_test_equipment_icon.jpg'
        self.assertEqual(profile['equipment'][0]['icon_url'], expected_icon_url)

        report = parse_simc_html_report('''
            <div class="player">
              <h2>TooltipTest: 100 dps</h2>
              <div class="player-section">
                <h3>Gear</h3>
                <table class="sc">
                  <tr><th>Slot</th><th>Item</th></tr>
                  <tr><td>Head</td><td><a href="https://www.wowhead.com/item=249952?ilvl=289">Night Ender's Tusks</a></td></tr>
                </table>
              </div>
            </div>
        ''')
        gear = next(section for section in report['sections'] if section['key'] == 'gear')
        item_cell = gear['tables'][0]['rows'][1][1]
        self.assertEqual(item_cell['item']['display_description'], '装备：中文装备属性与特效。')
        self.assertEqual(item_cell['item']['icon_url'], expected_icon_url)

        shared_js = (ROOT / 'static/shared/js/wow-item-tooltip.js').read_text(encoding='utf-8')
        shared_css = (ROOT / 'static/shared/css/wow-item-tooltip.css').read_text(encoding='utf-8')
        profile_js = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        result_js = (ROOT / 'static/dashboard/js/simc-result-report.js').read_text(encoding='utf-8')
        benchmark_js = (ROOT / 'static/portal/js/simc-benchmarks.js').read_text(encoding='utf-8')
        templates = '\n'.join(
            (ROOT / path).read_text(encoding='utf-8')
            for path in (
                'templates/dashboard/index.html',
                'templates/dashboard/simc_detail.html',
                'templates/portal/simc_benchmark_results.html',
            )
        )
        for source in (profile_js, result_js, benchmark_js):
            self.assertIn('data-wow-item-tooltip', source)
            self.assertIn('display_description', source)
            self.assertIn('icon_url', source)
            self.assertIn('wow-item-icon', source)
        for token in ('pointerover', 'focusin', 'click', 'role="tooltip"'):
            self.assertIn(token, shared_js)
        self.assertIn('.wow-item-tooltip', shared_css)
        self.assertGreaterEqual(templates.count('shared/js/wow-item-tooltip.js'), 3)
        self.assertGreaterEqual(templates.count('shared/css/wow-item-tooltip.css'), 3)
