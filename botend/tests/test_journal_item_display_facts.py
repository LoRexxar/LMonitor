from django.test import TestCase

from botend.models import WowItemSnapshot
from botend.services import wow_item_catalog_import as catalog


class ItemDisplayFactsMergeTests(TestCase):
    def test_verified_display_gaps_merge_without_replacing_item_facts(self):
        item = WowItemSnapshot.objects.create(
            item_id=37630, name='Moorabi Cloak', source='wago',
            quality=4, inventory_type=16, metadata={'preserved': {'build': 'original'}},
        )
        self.assertTrue(catalog.merge_item_display_facts(
            item, name_zh='莫拉比斗篷', icon='inv_misc_cape_19',
            provenance={'source': 'wowhead-tooltip', 'locale': 'zhCN', 'url': 'https://nether.wowhead.com/tooltip/item/37630?locale=zhcn'},
        ))
        item.refresh_from_db()
        self.assertEqual(item.name_zh, '莫拉比斗篷')
        self.assertEqual(item.icon, 'inv_misc_cape_19')
        self.assertEqual((item.name, item.source, item.quality, item.inventory_type),
                         ('Moorabi Cloak', 'wago', 4, 16))
        self.assertEqual(item.metadata['preserved'], {'build': 'original'})
        self.assertEqual(item.metadata['display_facts']['name_zh']['source'], 'wowhead-tooltip')
        before = item.updated_at
        self.assertFalse(catalog.merge_item_display_facts(
            item, name_zh='不同名称', icon='inv_misc_cape_20',
            provenance={'source': 'another-source'},
        ))
        item.refresh_from_db()
        self.assertEqual(item.updated_at, before)
        self.assertEqual((item.name_zh, item.icon), ('莫拉比斗篷', 'inv_misc_cape_19'))
        for kwargs in ({'icon': 'inv_misc_questionmark'}, {'icon': '../bad'}, {'name_zh': 'English'}, {'icon': 'valid_icon', 'provenance': {}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                catalog.merge_item_display_facts(item, **{'provenance': {'source': 'source'}, **kwargs})
        item.refresh_from_db()
        self.assertEqual(item.updated_at, before)

    def test_historical_official_chinese_is_imported_as_localized_name(self):
        loot = {'item_id': 37630, 'name': '莫拉比斗篷', 'source': 'wago-localization-12.0.5.67823',
                'class_id': 4, 'subclass_id': 1, 'slot': 16, 'quality': 4}
        catalog.upsert_journal_base_item_facts([{'encounters': [{'loot': [loot]}]}], build='12.1.0.69587')
        item = WowItemSnapshot.objects.get(item_id=37630)
        self.assertEqual(item.name_zh, '莫拉比斗篷')
        catalog.upsert_journal_base_item_facts([{'encounters': [{'loot': [{**loot, 'item_id': 37631, 'name': 'English only'}]}]}], build='12.1.0.69587')
        self.assertEqual(WowItemSnapshot.objects.get(item_id=37631).name_zh, '')
