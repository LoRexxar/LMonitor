import json
from io import StringIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkPanel, SimcContentTemplate, SimcProfile,
)
from botend.services.simc_benchmark_config import resolve_default_benchmark_resources
from botend.services.simc_player_config import SUPPORTED_SIMC_SPEC_IDENTITIES


FIXTURE = Path(__file__).parent / 'fixtures' / 'mid1_trinkets.json'


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

    def test_dry_run_prints_full_plan_without_writes_and_upsert_is_idempotent(self):
        self.seed_resources()
        stdout = StringIO()
        call_command('sync_midnight_trinket_benchmark', fixture=str(FIXTURE),
                     owner_id=self.owner_id, dry_run=True, stdout=stdout)
        first_line = stdout.getvalue().splitlines()[0]
        plan = json.loads(first_line)
        self.assertEqual((plan['spec_count'], plan['scenario_count'],
                          plan['candidate_count'], plan['case_count']),
                         (40, 3, 66, 120))
        self.assertEqual(plan['run_count'], sum(row['run_count'] for row in plan['specs']))
        self.assertEqual(len(plan['specs']), 40)
        self.assertFalse(SimcBenchmarkPanel.objects.exists())

        for _ in range(2):
            call_command('sync_midnight_trinket_benchmark', fixture=str(FIXTURE),
                         owner_id=self.owner_id, stdout=StringIO())
        panel = SimcBenchmarkPanel.objects.get(slug='midnight-s1-trinkets')
        self.assertEqual((panel.specs.count(), panel.scenarios.count(), panel.candidates.count()),
                         (40, 3, 66))
        self.assertEqual(list(panel.scenarios.order_by('display_order', 'id').values_list(
            'key', 'simulation_params__fight_style', 'simulation_params__desired_targets'
        )), [
            ('castingpatchwerk', 'CastingPatchwerk', 1),
            ('castingpatchwerk3', 'CastingPatchwerk', 3),
            ('castingpatchwerk5', 'CastingPatchwerk', 5),
        ])

    def test_existing_slug_owned_by_another_user_is_rejected(self):
        SimcBenchmarkPanel.objects.create(name='Other', slug='midnight-s1-trinkets', created_by_id=1)
        with self.assertRaisesMessage(CommandError, 'different owner'):
            call_command('sync_midnight_trinket_benchmark', fixture=str(FIXTURE),
                         owner_id=self.owner_id, dry_run=True)
