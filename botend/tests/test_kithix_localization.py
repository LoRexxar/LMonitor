"""真实离线制品的中文更新、数值保留和三入口回归。"""
from copy import deepcopy
from pathlib import Path
import tempfile
from uuid import uuid4
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase

from botend.journal_models import JournalRelease, JournalState
from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.portal.adventure_journal import catalog_data, detail_data, instance_source
from botend.services.kithix_localization import ITEM_IDS, import_localization, load_localization, localize_effect
from botend.services.ptr_journal_gear_overlay import import_ptr_journal_gear_overlay, preserve_active_ptr_journal_overlay
from botend.services.spec_stats_service import _compute_gear_popularity, _normalize_gear_items
from botend.services.wow_item_display import load_item_tooltip_metadata, refresh_aggregate_equipment
from botend.tests import test_ptr_journal_gear_overlay as overlay_tests
from botend.tests.journal_snapshot_fixtures import isolate_journal_snapshots, warm_journal


class KithixLocalizationTests(TestCase):
    def setUp(self):
        isolate_journal_snapshots(self)
        overlay_tests.PtrJournalGearOverlayTests.setUp(self)
        self.original = Path(settings.BASE_DIR) / 'botend/data/ptr_kithix_unbound_12_1_5.json'
        self.artifact = self.original.with_name('kithix_localization_12_1_5.json')
        import_ptr_journal_gear_overlay(self.original, apply=True)
        self.previous = JournalState.objects.get().active_release
        self.backup = Path(settings.BASE_DIR) / '.cache/kithix-localization-tests' / f'{uuid4()}.json'

    def apply(self):
        return import_localization(self.artifact, apply=True, backup_path=self.backup)

    def test_preview_does_not_write_and_apply_is_idempotent(self):
        count = JournalRelease.objects.count()
        report = import_localization(self.artifact)
        self.assertEqual(report['更新变体数'], 180)
        self.assertEqual(report['更新物品数'], 13)
        self.assertEqual(JournalRelease.objects.count(), count)
        self.assertFalse(WowItemSnapshot.objects.get(item_id=281029).name_zh)
        self.assertTrue(self.apply()['已写入'])
        self.assertTrue(self.backup.is_file())
        self.assertTrue(self.apply()['无需重复更新'])
        self.assertEqual(JournalRelease.objects.count(), count + 1)
        self.assertFalse(self.previous.instances.get(journal_id=1324).payload['description'].startswith('基希'))

    def test_all_variant_values_and_unrelated_items_are_unchanged(self):
        fields = ('id', 'item_level', 'game_build', 'stats_json', 'bonus_ids', 'compatible_slots', 'metadata')
        before = list(WowItemVariantSnapshot.objects.order_by('pk').values(*fields))
        old = WowItemSnapshot.objects.get(item_id=190001)
        old_fields = (old.name, old.metadata, old.updated_at)
        self.apply()
        self.assertEqual(before, list(WowItemVariantSnapshot.objects.order_by('pk').values(*fields)))
        old.refresh_from_db()
        self.assertEqual(old_fields, (old.name, old.metadata, old.updated_at))
        for variant in WowItemVariantSnapshot.objects.filter(item__item_id__in=ITEM_IDS):
            self.assertNotRegex(variant.effects_json[0]['description_zh'], r'[A-Za-z]{3,}|\$')

    def test_handbook_is_current_season_and_has_chinese_tooltip_without_ptr_badge(self):
        self.apply()
        release = JournalState.objects.get().active_release
        self.assertEqual(instance_source(release, 1324)['key'], 'current')
        request = RequestFactory().get('/portal/adventure-journal/')
        warm_journal(1324)
        self.assertIn(1324, {row['id'] for row in catalog_data(request)['instances']})
        detail = detail_data(RequestFactory().get('/', {'difficulty': 14}), 1324)
        self.assertEqual(detail['boss']['name'], '基希克斯')
        self.assertIn('基希克斯', detail['instance']['description'])
        self.assertTrue(detail['loot'])
        self.assertEqual(detail['loot'][0]['name'], '虚空编织者护腿')
        self.assertEqual(detail['source']['key'], 'current')
        self.assertEqual(detail['loot'][0]['details']['source'], 'LMonitor 装备目录')
        # 赛季徽标不能冒充 PTR；历史数值来源必须继续明确保留。
        self.assertIn('PTR 12.1.5.69594', detail['loot'][0]['details']['note'])
        response = self.client.get('/portal/adventure-journal/1324/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'journal-source-badge--current')
        self.assertNotContains(response, 'journal-source-badge--ptr')
        response = self.client.get('/portal/api/adventure-journal/1324/tooltip/item/281029/', {'difficulty': 14})
        self.assertEqual(response.status_code, 200)
        self.assertIn('虫群召唤者指环', response.json()['name'])
        self.assertEqual(response.json()['source'], 'LMonitor 装备目录')
        self.assertIn('PTR 12.1.5.69594', response.json()['note'])
        with patch('botend.services.journal_tooltip.requests.Session', side_effect=AssertionError('不应联网')):
            warm_journal(1324)
            response = self.client.get('/portal/api/adventure-journal/1324/tooltip/item/281615/', {'difficulty': 14})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['name'], '织影者的炉石')
        sections = release.instances.get(journal_id=1324).encounters.get(journal_id=2896).payload['sections']
        spell = next(row['spell_id'] for row in sections if row['spell_id'] and 14 in row['difficulty_ids'])
        response = self.client.get(f'/portal/api/adventure-journal/1324/tooltip/spell/{spell}/', {'difficulty': 14})
        self.assertEqual(response.status_code, 200)
        self.assertIn('build=12.1.5.70077', response.json()['url'])

    def test_gear_and_cached_aggregate_read_current_localization(self):
        self.apply()
        display = load_item_tooltip_metadata([{'item_id': 281029, 'item_level': 292}])[0]
        self.assertEqual(display['display_name'], '虫群召唤者指环')
        self.assertIn('酸巢灾虫', display['tooltip'])
        payload = {'boss_id': 281029, 'name': '保留首领原名', 'detail': {'gear_popularity': {'手指': [
            {'itemID': 281029, 'itemLevel': 292, 'name': '旧英文', 'display_description': '旧英文', 'count': 9, 'pct': 45.0}
        ]}}}
        refresh_aggregate_equipment(payload)
        item = payload['detail']['gear_popularity']['手指'][0]
        self.assertIn('酸巢灾虫', item['display_description'])
        self.assertEqual((item['count'], item['pct'], item['itemLevel']), (9, 45.0, 292))
        self.assertEqual(payload['name'], '保留首领原名')
        from botend.portal.spec_detail_views import _load_json
        from botend.services.spec_stats_snapshot import publish_projection
        from botend.services.simc_benchmark_result_snapshot import _write
        # 后台先补全中文再发布真实详情文件；读取期不再访问目录或修改正文。
        boss = {'boss_id': payload['boss_id'], 'name': payload['name'], **payload['detail']}
        with tempfile.TemporaryDirectory(prefix='kithix-aggregate-') as root:
            path = Path(root) / str(self.season.pk) / 'Warrior' / 'Arms' / 'raid.json'
            publish_projection(path, {'zone_groups': [{'zone_id': 1, 'bosses': [boss]}]}, _write)
            with patch('botend.portal.spec_detail_views.AGGREGATED_DIR', root), self.assertNumQueries(0):
                loaded = _load_json(self.season.pk, 'Warrior', 'Arms', 'raid.json', 'raid-5-281029')
        loaded_boss = loaded['zone_groups'][0]['bosses'][0]
        self.assertEqual(loaded_boss, boss)
        self.assertIn('酸巢灾虫', loaded_boss['gear_popularity']['手指'][0]['tooltip'])
        rows = _normalize_gear_items([{'id': 281029, 'itemLevel': 292, 'slot': 'finger1'}])
        self.assertIn('酸巢灾虫', rows[0]['display_description'])
        popularity = _compute_gear_popularity([{'gear_json': [{'id': 281029, 'itemLevel': 292, 'slot': 'finger1'}]}])
        self.assertIn('酸巢灾虫', next(iter(popularity.values()))[0]['display_description'])

    def test_current_handbook_follows_catalog_refresh_in_page_and_details(self):
        self.apply()
        item_ids = [281056, 281215, 280617, 280799, 280835]
        # 模拟中央目录更新构建和来源后，手册发布仍保留旧的掉落关系构建。
        for variant in WowItemVariantSnapshot.objects.filter(item__item_id__in=item_ids):
            variant.game_build = '12.1.5.70077'
            variant.metadata = {'ptr_preview': True, 'tooltip_source': {'provider': 'wowhead'}}
            for effect in variant.effects_json:
                effect.pop('game_build', None)
            variant.save(update_fields=['game_build', 'metadata', 'effects_json'])
        warm_journal(1324)
        with patch('botend.services.journal_tooltip.requests.Session', side_effect=AssertionError('不应联网')):
            page = self.client.get('/portal/adventure-journal/1324/', {'difficulty': 14, 'tab': 'loot'})
            self.assertEqual(page.status_code, 200)
            rows = {row['item_id']: row['details'] for row in page.context['loot']}
            for item_id in item_ids:
                expected = load_item_tooltip_metadata([{
                    'item_id': item_id, 'allow_default_variant': True, 'default_variant_order': 'lowest',
                }])[0]
                self.assertTrue(rows[item_id]['complete'])
                self.assertEqual(rows[item_id]['stats'], expected['stat_lines'])
                self.assertEqual(rows[item_id]['effects'], expected['effects'])
                response = self.client.get(
                    f'/portal/api/adventure-journal/1324/tooltip/item/{item_id}/', {'difficulty': 14},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), rows[item_id])
                self.assertContains(page, f'data-loot-id="{item_id}" data-details-loaded="true"')

    def test_sync_retains_season_override_when_retail_includes_same_instance(self):
        self.apply()
        release = JournalState.objects.get().active_release
        rows = [{'id': 1324, 'tier_ids': [516], 'encounters': [{'sections': [], 'loot': []}]}]
        catalog = deepcopy(release.manifest['catalog'])
        result = preserve_active_ptr_journal_overlay(release, rows, catalog, {}, '12.1.5.70100')
        current = next(t['id'] for t in catalog['tiers'] if t['order'] == 9000)
        self.assertIn(current, result[0][0]['tier_ids'])
        self.assertEqual(result[3], {})

    def test_unknown_effect_rejects_whole_update_and_failure_rolls_back(self):
        variant = WowItemVariantSnapshot.objects.filter(item__item_id=281029).first()
        effects = deepcopy(variant.effects_json)
        effects[0]['spell_id'] = 99999999
        variant.effects_json = effects
        variant.save()
        with self.assertRaisesRegex(ValueError, '没有官方中文'):
            self.apply()
        self.assertFalse(WowItemSnapshot.objects.get(item_id=281029).name_zh)
        self.assertEqual(JournalState.objects.get().active_release_id, self.previous.pk)
        self.assertFalse(self.backup.exists())

    def test_database_error_rolls_back_items_and_release(self):
        with patch.object(WowItemVariantSnapshot, 'save', side_effect=RuntimeError('模拟写入失败')):
            with self.assertRaisesRegex(RuntimeError, '模拟写入失败'):
                self.apply()
        self.assertFalse(WowItemSnapshot.objects.get(item_id=281029).name_zh)
        self.assertEqual(JournalState.objects.get().active_release_id, self.previous.pk)
        self.assertTrue(self.backup.is_file())

    def test_changed_translation_tokens_are_rejected(self):
        _payload, _row, _catalog, _items, spells, _digest = load_localization(self.artifact)
        effect = WowItemVariantSnapshot.objects.filter(item__item_id=281029).first().effects_json[0]
        with self.assertRaisesRegex(ValueError, '变量不一致'):
            localize_effect(effect, spells[effect['spell_id']]['Description_lang'] + '$s9', '12.1.5.70077')
