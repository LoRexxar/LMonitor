from django.test import SimpleTestCase, TestCase
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.journal_tooltip import cached_tooltip
from botend.portal.adventure_journal import _present_tooltip


class JournalStoredTooltipProvenanceTests(TestCase):
    def test_unversioned_wowhead_snapshot_does_not_borrow_journal_build(self):
        season = SeasonMeta.objects.create(
            season_key='provenance-test', season_name='Provenance', is_active=True,
            mplus_zone_id=1, raid_zone_id=2, gear_batch_key='provenance-batch',
        )
        item = WowItemSnapshot.objects.create(item_id=280835, name_zh='日蚀临世之印', catalog_type='equipment')
        WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key=season.gear_batch_key,
            variant_key='saved-tooltip', variant_type='drop_equipment', item_level=292,
            data_branch='ptr', stats_json={'intellect': 121},
            metadata={'ptr_preview': True, 'tooltip_source': {'provider': 'wowhead', 'branch': 'ptr-2'}},
        )
        result = cached_tooltip('item', item.item_id, 14, '12.1.5.69594', use_current_catalog=True)
        self.assertNotIn('12.1.5.69594', result['note'])
        self.assertIn('Wowhead', result['note'])
        self.assertIn('未记录客户端构建', result['note'])
        self.assertNotIn('SimulationCraft', result['source'])
        self.assertEqual(result['stats'], ['+121 智力'])


class JournalTooltipProvenanceTests(SimpleTestCase):
    def test_current_season_label_preserves_equipment_snapshot_version(self):
        data = {
            'source': 'LMonitor PTR DB2 + SimulationCraft',
            'item_level': 292,
            'note': '数值为 PTR 12.1.5.69594 的参考装等 292 快照，不代表所选难度的初始掉落装等。',
        }
        shown = _present_tooltip(data, {'key': 'current'})
        self.assertEqual(shown['note'], data['note'])
        self.assertEqual(data['source'], 'LMonitor PTR DB2 + SimulationCraft')
