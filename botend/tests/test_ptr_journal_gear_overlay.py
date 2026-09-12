from pathlib import Path

from django.conf import settings
from django.test import TestCase

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.ptr_journal_gear_overlay import import_ptr_journal_gear_overlay


class PtrJournalGearOverlayTests(TestCase):
    def setUp(self):
        release = JournalRelease.objects.create(
            build='12.1.0.69299',
            status='completed',
            manifest={'tables': {'JournalInstance': {'source_build': '12.1.0.69299'}}},
            report={'instances': 1, 'encounters': 1, 'sections': 1, 'loot': 1},
        )
        instance = JournalInstance.objects.create(
            release=release,
            journal_id=10,
            name='正式服测试团本',
            kind='raid',
            expansion=1200,
            payload={
                'id': 10,
                'name': '正式服测试团本',
                'kind': 'raid',
                'expansion': 1200,
                'tier_ids': [70],
                'difficulty_ids': [14],
                'boss_counts': {'14': 1},
            },
        )
        JournalEncounter.objects.create(
            instance=instance,
            journal_id=30,
            name='正式服测试首领',
            order=1,
            payload={'id': 30, 'name': '正式服测试首领', 'order': 1, 'sections': [{}], 'loot': [{}]},
        )
        JournalState.objects.create(key='wow-zhCN', active_release=release)
        self.season = SeasonMeta.objects.create(
            season_key='ptr-overlay-test',
            season_name='PTR Overlay Test',
            is_active=True,
            mplus_zone_id=1,
            raid_zone_id=2,
            game_build='12.1.0.69299',
            gear_batch_key='active-live-batch',
            gear_sync_status='ready',
        )
        live_item = WowItemSnapshot.objects.create(
            item_id=190001,
            name='Existing Live Item',
            quality=4,
            catalog_type='equipment',
            inventory_type=5,
            slot_key='chest',
            item_class_id=4,
            item_subclass_id=1,
            armor_type='布甲',
            allowable_class_mask=-1,
        )
        WowItemVariantSnapshot.objects.create(
            season=self.season,
            batch_key='active-live-batch',
            game_build='12.1.0.69299',
            item=live_item,
            variant_key='live-existing',
            variant_type='drop_equipment',
            item_level=300,
            compatible_slots=['chest'],
            stats_json={'intellect': 100},
            source_json=[{'type': 'raid'}],
        )

    def test_overlay_preserves_live_journal_and_appends_preview_gear_to_active_catalog(self):
        artifact = Path(settings.BASE_DIR) / 'botend' / 'data' / 'ptr_kithix_unbound_12_1_5.json'

        plan = import_ptr_journal_gear_overlay(artifact, apply=False)

        self.assertFalse(plan['applied'])
        self.assertEqual(JournalState.objects.get(pk='wow-zhCN').active_release.build, '12.1.0.69299')
        self.assertFalse(WowItemSnapshot.objects.filter(item_id=281235).exists())

        report = import_ptr_journal_gear_overlay(artifact, apply=True)

        active_release = JournalState.objects.get(pk='wow-zhCN').active_release
        self.assertNotEqual(active_release.build, '12.1.0.69299')
        self.assertEqual(
            set(active_release.instances.values_list('journal_id', flat=True)),
            {10, 1324},
        )
        self.assertTrue(
            JournalEncounter.objects.filter(
                instance__release=active_release,
                instance__journal_id=10,
                journal_id=30,
            ).exists()
        )
        self.assertTrue(
            JournalEncounter.objects.filter(
                instance__release=active_release,
                instance__journal_id=1324,
                journal_id=2896,
            ).exists()
        )
        self.assertEqual(report['journal']['added'], 1)
        self.assertEqual(report['gear']['items'], 10)
        self.assertEqual(report['gear']['variants'], 180)

        item = WowItemSnapshot.objects.get(item_id=281235)
        self.assertTrue(item.metadata['ptr_preview'])
        variants = WowItemVariantSnapshot.objects.filter(
            item=item,
            season=self.season,
            batch_key='active-live-batch',
        )
        self.assertEqual(variants.count(), 18)
        self.assertTrue(all(v.metadata['stats_status'] == 'awaiting_same_build_simc' for v in variants))
        self.assertTrue(all(v.stats_json == {} for v in variants))

        response = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Mage',
            'spec': 'Fire',
            'slot': 'chest',
            'source': 'raid',
        })
        self.assertEqual(response.status_code, 200)
        catalog_item = next(row for row in response.json()['items'] if row['item_id'] == 281235)
        self.assertEqual(catalog_item['name'], "Voidweaver's Vestments")
        self.assertTrue(catalog_item['metadata']['ptr_preview'])
        self.assertTrue(all(v['metadata']['stats_status'] == 'awaiting_same_build_simc'
                            for v in catalog_item['variants']))

        release_count = JournalRelease.objects.count()
        repeated = import_ptr_journal_gear_overlay(artifact, apply=True)
        repeated_release = JournalState.objects.get(pk='wow-zhCN').active_release
        self.assertTrue(repeated['already_applied'])
        self.assertEqual(JournalRelease.objects.count(), release_count)
        self.assertEqual(repeated['journal']['added'], 0)
        self.assertEqual(repeated['journal']['replaced'], 1)
        self.assertEqual(repeated_release.build, '12.1.0.69299+ptr-12.1.5.69594')
        self.assertEqual(repeated_release.instances.filter(journal_id=1324).count(), 1)
        self.assertEqual(WowItemSnapshot.objects.filter(item_id__in=[
            280617, 280799, 280835, 281029, 281056,
            281215, 281235, 281236, 281238, 281239,
        ]).count(), 10)
        self.assertEqual(WowItemVariantSnapshot.objects.filter(
            season=self.season,
            batch_key='active-live-batch',
            item__metadata__ptr_preview=True,
        ).count(), 180)
        self.assertTrue(WowItemVariantSnapshot.objects.filter(
            season=self.season,
            batch_key='active-live-batch',
            item__item_id=190001,
            variant_key='live-existing',
        ).exists())

        ptr_catalog = self.client.get('/portal/api/adventure-journal/', {'tier': ''}).json()
        ptr_instance = next(row for row in ptr_catalog['instances'] if row['id'] == 1324)
        live_instance = next(row for row in ptr_catalog['instances'] if row['id'] == 10)
        self.assertEqual(ptr_instance['source'], {
            'key': 'ptr', 'label': 'PTR 12.1.5', 'build': '12.1.5.69594',
        })
        self.assertEqual(live_instance['source'], {
            'key': 'retail', 'label': '正式服 12.1.0', 'build': '12.1.0.69299',
        })
        detail = self.client.get('/portal/adventure-journal/1324/')
        self.assertContains(detail, 'PTR 12.1.5')
        self.assertNotContains(detail, '正式服 12.1.0.69299+ptr-12.1.5.69594')

    def test_overlay_refuses_to_replace_an_empty_live_catalog(self):
        WowItemVariantSnapshot.objects.all().delete()
        artifact = Path(settings.BASE_DIR) / 'botend' / 'data' / 'ptr_kithix_unbound_12_1_5.json'

        with self.assertRaisesMessage(ValueError, '活动装备批次没有现存正式服装备'):
            import_ptr_journal_gear_overlay(artifact, apply=False)
