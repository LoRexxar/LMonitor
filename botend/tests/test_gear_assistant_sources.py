import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.services.gear_assistant import _current_pool
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class GearAssistantSourcesTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        release = JournalRelease.objects.create(build='source-test', status='complete')
        JournalState.objects.create(key='wow-zhCN', active_release=release)
        self.raid = JournalInstance.objects.create(release=release, journal_id=1320, name='Raid', kind='raid')
        for order, encounter_id in enumerate((11, 12, 2883, 2895), 1):
            JournalEncounter.objects.create(instance=self.raid, journal_id=encounter_id, order=order,
                                           name=f'Boss {order}', payload={'difficulty_ids': [14, 15, 16, 17]})
        other = JournalInstance.objects.create(release=release, journal_id=1317, name='Story raid', kind='raid')
        JournalEncounter.objects.create(instance=other, journal_id=2849, name='Story', order=100,
                                       payload={'difficulty_ids': [14, 15, 233]})
        self.hero.upgrade_track = 'hero'
        self.hero.source_json = [{'type': 'raid', 'instance_id': 1320, 'encounter_id': 2895}]
        self.hero.save(update_fields=['upgrade_track', 'source_json'])
        self.myth.source_json = self.hero.source_json
        self.myth.save(update_fields=['source_json'])
        # A higher myth version of the very same item must fall back to hero.
        self.myth = self.hero.__class__.objects.create(
            item=self.hero.item, season=self.season, batch_key=self.season.gear_batch_key,
            variant_key='source-myth-final', variant_type=self.hero.variant_type,
            item_level=self.hero.item_level + 30, upgrade_track='myth',
            compatible_slots=['head'], source_json=self.hero.source_json, stats_json=self.hero.stats_json,
        )

    def highest(self, allow):
        rows, _ = _current_pool('Warrior', 'Fury', allow_mythic_last_two=allow)
        return next(row for row in rows if row.item_id == self.hero.item_id)

    def test_filter_before_highest_level_dedup_preserves_lower_difficulty(self):
        self.assertEqual(self.highest(True).id, self.myth.id)
        self.assertEqual(self.highest(False).id, self.hero.id)
        self.assertEqual(self.highest(False).stats_json, self.hero.stats_json)

    def test_early_boss_dungeon_and_non_mythic_raid_are_not_filtered(self):
        for source in (
            {'type': 'raid', 'instance_id': 1320, 'encounter_id': 11},
            {'type': 'mythic_plus'},
            {'type': 'raid', 'instance_id': 1320, 'encounter_id': -97, 'encounter': 'Trash Drop'},
            {'type': 'raid', 'instance_id': 1317, 'encounter_id': 2849},
        ):
            with self.subTest(source=source):
                self.myth.source_json = [source]
                self.myth.save(update_fields=['source_json'])
                self.assertEqual(self.highest(False).id, self.myth.id)

    def test_any_confirmed_legal_alternative_source_preserves_variant(self):
        self.myth.source_json += [{'type': 'raid', 'instance_id': 1320, 'encounter_id': 12}]
        self.myth.save(update_fields=['source_json'])
        self.assertEqual(self.highest(False).id, self.myth.id)

    def test_tier_set_canonical_sources_override_old_generic_source(self):
        self.hero.item.metadata = {'item_set_id': 2055}
        self.hero.item.slot_key = 'head'
        self.hero.item.save(update_fields=['metadata', 'slot_key'])
        JournalEncounter.objects.create(instance=self.raid, journal_id=2887, name='Tier boss', order=0,
                                       payload={'difficulty_ids': [14, 15, 16, 17]})
        self.myth.source_json = [{'type': 'raid', 'instance': 'Tier Set / Catalyst'}]
        self.myth.save(update_fields=['source_json'])
        self.assertEqual(self.highest(False).id, self.myth.id)

    def test_unknown_journal_cannot_silently_bypass_disabled_source(self):
        JournalState.objects.all().delete()
        self.assertEqual(self.highest(False).id, self.hero.id)
        self.assertEqual(self.highest(True).id, self.myth.id)

    def test_api_rejects_non_boolean_switch(self):
        user = get_user_model().objects.create_user(username='source-option')
        self.client.force_login(user)
        response = self.client.post('/portal/api/gear-assistant/optimize/', data=json.dumps({
            'target': {'crit': 25}, 'allow_mythic_last_two': 'false', 'use_ai': False,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('布尔', response.json()['error'])
