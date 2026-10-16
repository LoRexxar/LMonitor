"""中央特效跨入口、独立完整度及无重复写入的回归验证。"""
from copy import deepcopy
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import serialize_variant
from botend.services.journal_tooltip import cached_tooltip
from botend.services.wow_item_display import load_item_tooltip_metadata
from botend.services.wow_item_effects import sync_central_effects


class CentralItemEffectsTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='中央特效验证', season_name='中央特效验证',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='共用批次')
        self.item = WowItemSnapshot.objects.create(item_id=1234, name_zh='验证饰品',
            catalog_type='equipment', slot_key='trinket', inventory_type=12, quality=4)
        self.base = self.variant('赛季', 300, [{'spell_id': 900, 'trigger_type': 1,
            'description': 'Gain 100 Haste for 10 seconds.',
            'description_zh': '装备：获得100急速，持续10秒。'}])
        self.other = self.variant('手册', 250, [{'spell_id': 900, 'trigger_type': 1,
            'description': 'Gain 50 Haste for 10 seconds.'}], difficulty=True)

    def variant(self, key, level, effects, difficulty=False, branch='retail'):
        metadata = {'data_branch': branch}
        if difficulty:
            metadata['difficulty_drop'] = dict(schema=1, difficulty_id=1, item_context=1,
                instance_id=10, rule_build='12.1.0.69587', quality=3, provider='wago', bonus_ids=[100])
        return WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
            batch_key=self.season.gear_batch_key, variant_key=key, variant_type='drop_equipment',
            item_level=level, data_branch=branch, bonus_ids=[100], upgrade_track='hero',
            stats_json={'haste': level}, effects_json=effects, metadata=metadata)

    def sync(self):
        return sync_central_effects(season=self.season, notify=False)

    def journal(self, difficulty=1, branch='retail'):
        return cached_tooltip('item', self.item.item_id, difficulty, '12.1.0.69587',
            item_context=difficulty, instance_id=10, data_branch=branch)

    def test_common_text_keeps_target_values_for_gear_journal_and_simc(self):
        self.sync()
        self.other.refresh_from_db()
        self.item.refresh_from_db()
        self.other.item = self.item
        gear = serialize_variant(self.other)
        journal = self.journal()
        simc = load_item_tooltip_metadata([{'item_id': self.item.item_id, 'item_level': 300}])[0]
        self.assertEqual(gear['effects'][0]['description_zh'], '装备：获得50急速，持续10秒。')
        self.assertEqual(journal['effects'], ['装备：获得50急速，持续10秒。'])
        self.assertEqual(simc['effects'], ['装备：获得100急速，持续10秒。'])
        self.assertTrue(journal['complete'])

    def test_missing_difficulty_keeps_chinese_without_borrowing_values(self):
        self.sync()
        shown = self.journal(2)
        self.assertEqual(shown['status'], 'partial')
        self.assertEqual(shown['effects_status'], 'reference')
        self.assertIsNone(shown['item_level'])
        self.assertEqual(shown['stats'], [])
        self.assertTrue(shown['effects'])
        self.assertIn('急速', shown['effects'][0])
        self.assertNotRegex(shown['effects'][0], r'\d')
        self.assertIn('数值待确认', shown['effects'][0])

    def test_read_only_legacy_projection_needs_no_backfill_or_network(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('读取禁止抓取')):
            with CaptureQueriesContext(connection) as queries:
                shown = self.journal(2)
        self.assertTrue(shown['effects'])
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for q in queries))
        self.item.refresh_from_db()
        self.assertEqual(self.item.effects, {})

    def test_sync_is_idempotent_and_dry_run_does_not_write(self):
        call_command('sync_central_item_effects', item_id=[self.item.item_id], stdout=StringIO())
        self.item.refresh_from_db()
        self.assertEqual(self.item.effects, {})
        self.assertEqual(self.sync()['items'], 1)
        self.item.refresh_from_db()
        stamp = self.item.updated_at
        with CaptureQueriesContext(connection) as queries:
            report = self.sync()
        self.assertEqual((report['items'], report['variants']), (0, 0))
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for q in queries))
        self.item.refresh_from_db()
        self.assertEqual(stamp, self.item.updated_at)

    def test_branches_do_not_share_effects(self):
        self.sync()
        self.assertEqual(self.journal(2, 'ptr')['effects'], [])

    def test_changed_source_values_do_not_use_stale_reference(self):
        self.sync()
        self.other.effects_json[0]['description'] = 'Gain 75 Haste for 10 seconds.'
        self.other.save(update_fields=['effects_json'])
        shown = self.journal()
        self.assertEqual(shown['effects'], ['装备：获得75急速，持续10秒。'])

    def test_changed_mechanic_is_not_translated_by_similar_spell_id(self):
        self.sync()
        self.other.effects_json[0]['description'] = 'Deal 50 damage.'
        self.other.save(update_fields=['effects_json'])
        self.assertEqual(self.journal()['effects'], ['Deal 50 damage.'])

    def test_central_translation_edit_is_shared_without_rewriting_variants(self):
        self.sync()
        self.item.refresh_from_db()
        definitions = self.item.effects['branches']['retail']['definitions']
        for row in definitions.values():
            if row.get('template_zh'):
                row['template_zh'] = row['template_zh'].replace('获得', '提高')
        self.item.save(update_fields=['effects'])
        self.assertEqual(self.journal()['effects'], ['装备：提高50急速，持续10秒。'])
        self.assertEqual(self.sync()['items'], 0)
        self.assertEqual(self.journal()['effects'], ['装备：提高50急速，持续10秒。'])

    def test_empty_native_variant_never_inherits_another_effect(self):
        self.sync()
        self.other.effects_json = []
        self.other.save(update_fields=['effects_json'])
        with patch('botend.services.wow_item_display._native_journal_variant', return_value=True):
            shown = self.journal()
        self.assertEqual(shown['effects'], [])
        self.assertTrue(shown['complete'])

    def test_partial_effects_render_in_journal_template(self):
        self.sync()
        details = self.journal(2)
        html = render_to_string('portal/adventure_journal_detail.html', {
            'source': {'key': 'current', 'build': '12.1.0.69587'},
            'instance': {'name': '验证副本', 'id': 10}, 'tab': 'loot',
            'loot_catalog': [{'item_id': self.item.item_id, 'visible': True,
                'name': self.item.name_zh, 'details': details}],
        })
        self.assertIn('获得数值待确认急速', html)
        self.assertIn('此难度装等与属性待确认', html)

    def test_explicit_spell_templates_reuse_values_without_positional_guess(self):
        self.base.effects_json = [{'spell_id': 901, 'trigger_type': 1,
            'template': 'Gain $s1 Haste for $d.', 'template_zh': '获得$s1急速，持续$d。',
            'description': 'Gain 100 Haste for 10秒.', 'description_zh': '装备：获得100急速，持续10秒。'}]
        self.base.save(update_fields=['effects_json'])
        self.other.effects_json = [{'description': 'Gain 75 Haste for 10秒.'}]
        self.other.save(update_fields=['effects_json'])
        self.sync()
        self.assertEqual(self.journal()['effects'], ['装备：获得75急速，持续10秒。'])

    def test_empty_source_rows_do_not_shift_parameter_references(self):
        self.other.effects_json.insert(0, {})
        self.other.save(update_fields=['effects_json'])
        self.sync()
        self.assertEqual(self.journal()['effects'], ['装备：获得50急速，持续10秒。'])

    def test_conflicting_chinese_templates_do_not_guess_a_translation(self):
        conflicting = deepcopy(self.base.effects_json)
        conflicting[0]['description_zh'] = '装备：获得100暴击，持续10秒。'
        self.variant('矛盾证据', 310, conflicting)
        self.sync()
        self.assertEqual(self.journal()['effects'], ['Gain 50 Haste for 10 seconds.'])

    def test_repeated_equal_numbers_are_not_used_as_cross_language_variable_identity(self):
        self.base.effects_json[0].update(description='Gain 10 Haste for 10 seconds.',
                                        description_zh='装备：获得10急速，持续10秒。')
        self.base.save(update_fields=['effects_json'])
        self.sync()
        self.assertEqual(self.journal()['effects'], ['Gain 50 Haste for 10 seconds.'])

    def test_shared_text_survives_a_later_english_only_collection(self):
        self.sync()
        self.base.effects_json[0].pop('description_zh')
        self.base.save(update_fields=['effects_json'])
        self.sync()
        self.assertEqual(self.journal()['effects'], ['装备：获得50急速，持续10秒。'])

    def test_partial_snapshot_requires_actual_drop_scope_and_empty_unknown_stats(self):
        from botend.services.journal_loot_snapshot import _scope_entry, _validate_coverage
        self.sync()
        row = dict(item_id=self.item.item_id, faction='', condition_id=0, display_season_id=0,
                   slot=12, item_type='trinket', class_id=4, filter_classes=[], filter_specs=[],
                   sources=[{'id': 100}], details=self.journal(2))
        expected = [_scope_entry(row, equipment=True)]
        payload = {'loot': [row], 'loot_total': 1}
        self.assertTrue(_validate_coverage(payload, {}, expected)['complete'])
        row['details']['stats'] = ['+100 急速']
        with self.assertRaises(ValueError):
            _validate_coverage(payload, {}, expected)
        row['details']['stats'] = []
        row['sources'] = []
        with self.assertRaises(ValueError):
            _validate_coverage(payload, {}, expected)

    def test_periodic_collection_does_not_recollect_journal_variants(self):
        from botend.services import wow_data_update as update
        item = update.item_payload(self.item)
        item['variants'] = [update.variant_payload(self.base)]
        with patch.object(update, 'BranchCatalogSource') as factory, \
                patch.object(update.UpdateSource, 'facts', return_value=([], [])), \
                patch.object(update.UpdateSource, 'talents', return_value='.cache/未使用测试目录'), \
                patch.object(update.UpdateSource, 'talent_display'), \
                patch('botend.services.item_loot_specializations.prepare_specializations', return_value={}):
            factory.return_value.build.return_value = {'items': [item]}
            prepared = update.UpdateSource().prepare('retail', '12.1.0.69587', None, Mock())
        factory.return_value._enrich_wowhead.assert_called_once_with([], '12.1.0.69587')
        self.assertEqual(len(prepared['catalog']['items'][0]['variants']), 1)

    def test_different_activation_type_does_not_reuse_equip_translation(self):
        self.other.effects_json[0]['trigger_type'] = 0
        self.other.save(update_fields=['effects_json'])
        self.sync()
        self.assertEqual(self.journal()['effects'], ['Gain 50 Haste for 10 seconds.'])

    def test_notification_is_sent_once_and_only_after_commit_for_changed_data(self):
        with patch('botend.services.wow_item_effects.notify_effects_changed') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                sync_central_effects(season=self.season)
                notify.assert_not_called()
            notify.assert_called_once()
            notify.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                sync_central_effects(season=self.season)
            notify.assert_not_called()
