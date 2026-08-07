import json
from io import StringIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from botend.models import (
    SimcApl,
    SimcBackendBinary,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcContentTemplate,
    SimcProfile,
)
from botend.services.simc_benchmark_config import resolve_default_benchmark_resources
from botend.services.simc_player_config import SUPPORTED_SIMC_SPEC_IDENTITIES


FIXTURE = Path(__file__).parent / 'fixtures' / 'ptr_12_1_trinkets.json'


class MidnightTrinketSyncTests(TestCase):
    owner_id = 901

    def seed_resources(self):
        SimcBackendBinary.objects.update_or_create(
            identifier='production', defaults={'name': 'Production', 'is_active': True},
        )
        for class_name, spec in sorted(SUPPORTED_SIMC_SPEC_IDENTITIES):
            key = f'{class_name}_{spec}'
            SimcApl.objects.create(
                name=key, class_name=class_name, spec=key, content='actions=/auto_attack',
                source=SimcApl.SOURCE_SIMC_UPSTREAM, is_system=True,
                owner_user_id=None, is_active=True, is_selectable=True,
            )
            SimcContentTemplate.objects.create(
                name=key, class_name=class_name, spec=key, content='iterations=1000',
                source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                owner_user_id=None, is_active=True, is_selectable=True,
            )
            SimcProfile.objects.create(
                name=key, class_name=class_name, spec=key,
                source=SimcProfile.SOURCE_SIMC_UPSTREAM, user_id=None, is_active=True,
            )

    def test_resolution_fails_closed_when_a_default_is_missing(self):
        SimcBackendBinary.objects.update_or_create(
            identifier='production', defaults={'name': 'Production', 'is_active': True},
        )
        with self.assertRaisesMessage(ValidationError, 'missing'):
            resolve_default_benchmark_resources(['mage_fire'], self.owner_id)

    def test_dry_run_prints_plan_and_upsert_is_idempotent_without_execution(self):
        self.seed_resources()
        stdout = StringIO()
        call_command(
            'sync_midnight_trinket_benchmark', fixture=str(FIXTURE),
            owner_id=self.owner_id, dry_run=True, slug='ptr-12-1-mythic-trinkets',
            stdout=stdout,
        )
        plan = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            (plan['spec_count'], plan['scenario_count'], plan['candidate_count'], plan['case_count']),
            (32, 3, 49, 96),
        )
        self.assertEqual(plan['run_count'], 32 * 3 * 50)
        self.assertEqual(plan['unresolved_item_ids'], [
            267631, 270172, 274370, 274371, 277735, 279190,
        ])
        self.assertFalse(SimcBenchmarkPanel.objects.exists())
        self.assertFalse(SimcBenchmarkExecution.objects.exists())

        for _ in range(2):
            call_command(
                'sync_midnight_trinket_benchmark', fixture=str(FIXTURE),
                owner_id=self.owner_id, slug='ptr-12-1-mythic-trinkets', stdout=StringIO(),
            )
        panel = SimcBenchmarkPanel.objects.get(slug='ptr-12-1-mythic-trinkets')
        self.assertEqual(
            (panel.specs.count(), panel.scenarios.count(), panel.candidates.count()),
            (32, 3, 49),
        )
        fixture_items = {
            row['item_id']: row['item_level']
            for row in json.loads(FIXTURE.read_text())['items']
        }
        actual_items = {}
        fixture_names = {
            row['item_id']: row['name_zhcn']
            for row in json.loads(FIXTURE.read_text())['items']
        }
        for candidate in panel.candidates.all():
            swap = candidate.params['gear_swap']
            actual_items[swap['item_id']] = int(
                swap['raw_value'].split('ilevel=', 1)[1].split(',', 1)[0]
            )
            self.assertEqual(
                candidate.label,
                f'{fixture_names[swap["item_id"]]} · {actual_items[swap["item_id"]]}',
            )
            self.assertNotRegex(candidate.label, r' · (\d+) · \1$')
        self.assertEqual(actual_items, fixture_items)
        self.assertFalse(SimcBenchmarkExecution.objects.exists())

    def test_panel_id_updates_the_same_panel_even_when_requested_slug_differs(self):
        self.seed_resources()
        panel = SimcBenchmarkPanel.objects.create(
            name='Old MID1', slug='ptr-12-1-mythic-trinkets',
            created_by_id=self.owner_id,
        )
        call_command(
            'sync_midnight_trinket_benchmark', fixture=str(FIXTURE),
            owner_id=self.owner_id, panel_id=panel.pk,
            slug='ignored-new-slug', stdout=StringIO(),
        )
        panel.refresh_from_db()
        self.assertEqual(panel.slug, 'ptr-12-1-mythic-trinkets')
        self.assertEqual(SimcBenchmarkPanel.objects.count(), 1)
        self.assertEqual(panel.candidates.count(), 49)
        self.assertFalse(SimcBenchmarkExecution.objects.exists())

    def test_existing_slug_owned_by_another_user_is_rejected(self):
        SimcBenchmarkPanel.objects.create(
            name='Other', slug='ptr-12-1-mythic-trinkets', created_by_id=1,
        )
        with self.assertRaisesMessage(CommandError, 'different owner'):
            call_command(
                'sync_midnight_trinket_benchmark', fixture=str(FIXTURE),
                owner_id=self.owner_id, dry_run=True, slug='ptr-12-1-mythic-trinkets',
            )
