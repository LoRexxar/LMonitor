"""Named CDN icons and Journal FileDataIDs are different identities."""
from copy import deepcopy

from django.test import TestCase, override_settings

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import serialize_item
from botend.services.journal_tooltip import cached_tooltip
from botend.services.wow_data_branch import branch_item
from botend.services.wow_item_display import item_display_metadata, load_item_tooltip_metadata
from botend.services.wow_item_identity import identity_reference, project_item_identity
from botend.tests.test_wow_item_identity import NEW, fact


@override_settings(WOW_ICON_OSS_BASE_URL='https://oss.wowdaily.cn/wow_icons_oss')
class ItemIconProjectionTests(TestCase):
    def test_ptr_numeric_overlay_keeps_named_icon_through_real_consumers(self):
        season = SeasonMeta.objects.create(
            season_key='icon-regression', season_name='Icons', is_active=True,
            gear_batch_key='icon-regression', mplus_zone_id=1, raid_zone_id=2,
        )
        item = WowItemSnapshot.objects.create(
            item_id=281029, name='Acidbrood Scourge', icon='acidbroodscourge_8268560',
            catalog_type='equipment', slot_key='trinket',
            metadata={'branch_display': {'ptr': {'icon': '8268560', 'name': 'PTR name'}},
                      'journal_item': {'icon': 8268560}},
        )
        variant = WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key=season.gear_batch_key,
            data_branch='ptr', variant_key='ptr-icon', variant_type='drop_equipment',
            item_level=292, stats_json={'intellect': 121},
            effects_json=[{'description_zh': '装备：测试特效。'}],
            metadata={'data_branch': 'ptr'},
        )
        original = deepcopy(item.metadata)
        display = load_item_tooltip_metadata([{'item_id': item.item_id, 'allow_default_variant': True}])[0]
        catalog = serialize_item(item, [variant])
        journal = cached_tooltip('item', item.item_id, 14, '', use_current_catalog=True)
        for payload, size in ((display, 'small'), (catalog, 'medium')):
            with self.subTest(consumer=size):
                self.assertEqual(payload['icon'], item.icon)
                self.assertEqual(payload['icon_url'], f'https://oss.wowdaily.cn/wow_icons_oss/{size}/{item.icon}.jpg')
        self.assertEqual(journal['icon'], display['icon_url'])
        self.assertEqual(display['name'], 'PTR name')
        item.refresh_from_db()
        self.assertEqual(item.metadata, original)
        self.assertEqual(item.metadata['journal_item']['icon'], 8268560)

    def test_unmapped_fdid_remains_available_without_fabricating_cdn_name(self):
        for raw in ('8268560', 8268560):
            with self.subTest(raw=raw):
                item = WowItemSnapshot(item_id=281029, icon='', metadata={
                    'branch_display': {'ptr': {'icon': raw}}, 'journal_item': {'icon': 8268560}})
                projected = branch_item(item, 'ptr')
                display = item_display_metadata(item.item_id, projected)
                self.assertEqual(projected.icon, raw)
                self.assertEqual(display['icon'], raw)
                self.assertEqual(display['icon_url'], '')
                self.assertEqual(display['journal_item']['icon'], 8268560)
                self.assertEqual(item.icon, '')

    def test_identity_projection_uses_branch_icon_without_changing_frozen_identity(self):
        ref = identity_reference(fact(NEW), is_ptr=True)
        item = WowItemSnapshot(item_id=281235, icon='inv_base_icon', metadata={
            'branch_display': {'ptr': {'icon': 'inv_ptr_icon'}}})
        before = deepcopy((ref, item.metadata))
        projected = project_item_identity(item, ref)
        self.assertEqual(projected.icon, 'inv_ptr_icon')
        self.assertEqual(projected._resolved_identity, ref)
        self.assertEqual((ref, item.metadata), before)
        # Numeric source facts do not replace a known named icon, but remain stored.
        item.metadata['branch_display']['ptr']['icon'] = '8268560'
        self.assertEqual(project_item_identity(item, ref).icon, 'inv_base_icon')
        self.assertEqual(item.metadata['branch_display']['ptr']['icon'], '8268560')
