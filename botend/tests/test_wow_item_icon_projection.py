"""Named CDN icons and Journal FileDataIDs are different identities."""
from copy import deepcopy
import hashlib
from io import BytesIO

from PIL import Image

from botend.services import wow_item_catalog_import as catalog

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
    def test_verified_item_asset_survives_branch_identity_and_catalog_projection(self):
        item = WowItemSnapshot.objects.create(
            item_id=281235, name='Original', icon='900001', source='wago-ptr-db2',
            metadata={'branch_display': {
                'ptr': {'icon': '900002'}, 'retail': {'icon': 'inv_old_branch'}},
                'display_facts': {'name_zh': {'source': 'preserve'}},
                'journal_item': {'icon': 900001}, 'unrelated_evidence': {'keep': [1, 2]}},
        )
        season = SeasonMeta.objects.create(
            season_key='asset', season_name='Asset', gear_batch_key='asset',
            mplus_zone_id=1, raid_zone_id=2,
        )
        variant = WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key='asset', variant_key='asset',
            game_build=NEW, data_branch='retail', item_level=321,
            stats_json={'intellect': 123}, effects_json=[{'description': 'Preserved effect'}],
        )
        before = WowItemSnapshot.objects.values().get(pk=item.pk)
        variant_before = WowItemVariantSnapshot.objects.values().get(pk=variant.pk)
        image = BytesIO()
        Image.new('RGB', (36, 36), 'red').save(image, format='JPEG')
        image_bytes = image.getvalue()
        icon = '900003'
        provenance = {
            'source': 'wowhead_tooltip', 'item_id': item.item_id,
            'tooltip_url': f'https://nether.wowhead.com/tooltip/item/{item.item_id}?dataEnv=1&locale=0',
            'tooltip_response': {'name': 'Verified item', 'icon': icon, 'quality': 4},
            'image_url': f'https://wow.zamimg.com/images/wow/icons/medium/{icon}.jpg',
        }
        self.assertEqual(item_display_metadata(item.item_id, item)['icon_url'], '')
        self.assertTrue(catalog.merge_item_icon_asset(
            item, icon=icon, image_bytes=image_bytes, provenance=provenance))
        item.refresh_from_db()
        asset = item.metadata['display_facts']['icon']
        self.assertEqual(asset['item_id'], item.item_id)
        self.assertEqual(asset['value'], icon)
        self.assertEqual((asset['format'], asset['width'], asset['height']), ('JPEG', 36, 36))
        self.assertEqual(asset['sha256'], hashlib.sha256(image_bytes).hexdigest())
        after = WowItemSnapshot.objects.values().get(pk=item.pk)
        for field in before.keys() - {'metadata', 'updated_at'}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after['metadata'], {**before['metadata'], 'display_facts': {
            **before['metadata']['display_facts'], 'icon': asset}})
        self.assertEqual(WowItemVariantSnapshot.objects.values().get(pk=variant.pk), variant_before)
        with self.assertNumQueries(0):
            self.assertFalse(catalog.merge_item_icon_asset(
                item, icon=icon, image_bytes=image_bytes, provenance=provenance))
            self.assertFalse(catalog.merge_item_icon_asset(
                item, icon=icon, image_bytes=image_bytes, provenance={**provenance,
                    'tooltip_response': {**provenance['tooltip_response'], 'name': 'Refreshed'}}))
        self.assertEqual(WowItemSnapshot.objects.values().get(pk=item.pk), after)
        for source in ('wago-ptr-db2', 'unrelated', ''):
            for branch in ('retail', 'ptr', 'beta'):
                with self.subTest(source=source, branch=branch):
                    item.source = source
                    projected = branch_item(item, branch)
                    ref = identity_reference(fact(NEW), is_ptr=branch == 'ptr')
                    frozen = project_item_identity(projected, ref)
                    self.assertEqual(frozen.metadata, {
                        'primary_stat_options': ref['primary_stat_options'],
                        'display_facts': {'icon': asset}})
                    self.assertEqual(frozen._resolved_identity, ref)
                    for row in (projected, frozen):
                        display = item_display_metadata(item.item_id, row)
                        self.assertEqual(display['icon'], icon)
                        self.assertEqual(display['icon_url'],
                            f'https://oss.wowdaily.cn/wow_icons_oss/small/{icon}.jpg')
                        output = serialize_item(row, [])
                        self.assertEqual(output['icon'], icon)
                        self.assertEqual(output['icon_url'],
                            f'https://oss.wowdaily.cn/wow_icons_oss/medium/{icon}.jpg')
        # Real variant serialization and bulk ORM readers consume the same mapping.
        variant = WowItemVariantSnapshot.objects.select_related('item').get(pk=variant.pk)
        self.assertEqual(serialize_item(variant.item, [variant])['icon'], icon)
        self.assertEqual(load_item_tooltip_metadata([{'item_id': item.item_id}])[0]['icon'], icon)
        for change in ({'item_id': item.item_id + 1}, {'item_id': True},
                       {'schema_version': 2}, {'value': '../bad'},
                       {'value': 'inv_misc_questionmark'}, {'sha256': ''}, {'width': 0},
                       {'provenance': {'source': 'truthy'}}):
            with self.subTest(invalid_mapping=change):
                item.metadata = {'display_facts': {'icon': {**asset, **change}}}
                item.source = 'wago'
                self.assertEqual(item_display_metadata(item.item_id, item)['icon_url'], '')
        for changes in ({'source': 'truthy'}, {'item_id': item.item_id + 1},
                        {'tooltip_response': {'icon': '900004', 'name': 'Wrong'}},
                        {'tooltip_url': provenance['tooltip_url'].replace(str(item.item_id), '1')},
                        {'image_url': provenance['image_url'].replace(icon, '900004')}):
            with self.subTest(invalid_provenance=changes), self.assertRaises(ValueError):
                catalog.merge_item_icon_asset(item, icon=icon, image_bytes=image_bytes,
                                              provenance={**provenance, **changes})
        png = BytesIO()
        Image.new('RGB', (36, 36)).save(png, format='PNG')
        wrong_size = BytesIO()
        Image.new('RGB', (18, 18)).save(wrong_size, format='JPEG')
        for invalid_bytes in (b'\xff\xd8\xffnot a JPEG', image_bytes[:100], png.getvalue(),
                              wrong_size.getvalue(), b'x' * (128 * 1024 + 1)):
            with self.subTest(invalid_image=len(invalid_bytes)), self.assertRaises(ValueError):
                catalog.merge_item_icon_asset(item, icon=icon, image_bytes=invalid_bytes,
                                              provenance=provenance)
        for invalid_icon in ('../bad', '900003.jpg', ' inv_icon', 'inv_misc_questionmark'):
            with self.subTest(invalid_icon=invalid_icon), self.assertRaises(ValueError):
                catalog.merge_item_icon_asset(item, icon=invalid_icon, image_bytes=image_bytes,
                                              provenance=provenance)
        self.assertEqual(WowItemSnapshot.objects.values().get(pk=item.pk), after)
        # Named mappings also win over older named/raw facts without changing them.
        item.refresh_from_db()
        named = 'inv_verified_asset'
        named_provenance = {**provenance,
            'tooltip_response': {**provenance['tooltip_response'], 'icon': named},
            'image_url': provenance['image_url'].replace(icon, named)}
        self.assertTrue(catalog.merge_item_icon_asset(
            item, icon=named, image_bytes=image_bytes, provenance=named_provenance))
        self.assertEqual(serialize_item(branch_item(item, 'retail'), [])['icon'], named)
        self.assertEqual(item.icon, before['icon'])

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

    def test_published_numeric_wowhead_basename_is_not_an_unmapped_fdid(self):
        # Wowhead's actual item 268240 response publishes "8095063" as its
        # icon basename, and that exact CDN JPEG exists; it is not a guessed URL.
        item = WowItemSnapshot(item_id=268240, icon='8095063',
            source='wowhead_live_tooltip', metadata={
                'branch_display': {'retail': {'icon': '8095063'}}})
        projected = branch_item(item, 'retail')
        display = item_display_metadata(item.item_id, projected)
        self.assertEqual(display['icon_url'], 'https://oss.wowdaily.cn/wow_icons_oss/small/8095063.jpg')
        item.metadata['branch_display']['retail']['icon'] = '8268560'
        self.assertEqual(item_display_metadata(item.item_id, branch_item(item, 'retail'))['icon_url'], '')
        item.metadata = {}
        item.source = 'wago'
        self.assertEqual(item_display_metadata(item.item_id, item)['icon_url'], '')

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
