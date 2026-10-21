"""Shared Benchmark ORM preparation; deliberately contains no test methods."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch
from botend.models import (
    SimcApl,
    SimcBackendBinary,
    SimcBenchmarkCandidate,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkScenario,
    SimcBenchmarkSpec,
    SimcContentTemplate,
    SimcProfile,
    SimcTalentString,
    SimulationRun,
    WowItemSnapshot,
)
from botend.services.simc_benchmark_execution import create_execution, reconcile_execution


def benchmark_trinket(item_id=123, **overrides):
    """Real central facts shared by execution and tooltip fixtures."""
    values = {
        'name': 'Test Trinket', 'catalog_type': 'equipment',
        'slot_key': 'trinket', 'inventory_type': 12,
        'item_class_id': 4, 'item_subclass_id': 0,
        'metadata': {'primary_stat_options': ['strength', 'agility', 'intellect']},
    }
    values.update(overrides)
    return WowItemSnapshot.objects.update_or_create(item_id=item_id, defaults=values)[0]


class BenchmarkExecutionFixtureMixin:
    user_id = 701

    def setUp(self):
        benchmark_trinket()
        self.backend = SimcBackendBinary.objects.create(
            identifier='benchmark-execution', name='Benchmark', is_active=True,
            current_version='a' * 40,
        )
        self.apl = SimcApl.objects.create(
            name='Fury APL', spec='warrior_fury', content='actions=/auto_attack',
            owner_user_id=self.user_id, is_active=True, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(b'actions=/auto_attack').hexdigest(),
            validation_revision='a' * 40, validation_game_build='12.0.1',
        )
        self.template = SimcContentTemplate.objects.create(
            name='Template', spec='warrior_fury', content='iterations=1000',
            owner_user_id=self.user_id,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id, name='Profile', class_name='warrior',
            spec='warrior_fury', is_active=True,
        )
        self.talent = SimcTalentString.objects.create(
            name='Slayer talents', spec='warrior_fury', talent='CYEAoonA',
            owner_user_id=self.user_id,
        )
        self.panel = SimcBenchmarkPanel.objects.create(
            name='Weekly', slug='weekly-execution', created_by_id=self.user_id,
            is_active=True, schedule_enabled=True,
        )
        spec = SimcBenchmarkSpec.objects.create(
            panel=self.panel, class_name='warrior', spec_key='warrior_fury',
            label='Fury', apl=self.apl, template=self.template, backend=self.backend,
        )
        self.benchmark_profile = SimcBenchmarkProfile.objects.create(
            panel_spec=spec, profile=self.profile, talent_string=self.talent,
            label='Raid profile',
        )
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='patchwerk', name='Patchwerk',
            simulation_params={'iterations': 1000},
        )
        SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='trinket', label='Trinket',
            candidate_type='gear_swap', params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': {'slot': 'trinket1', 'raw_value': ',id=123',
                              'item_id': 123, 'source': 'manual'},
                'simc_options': [
                    'midnight.crucible_of_erratic_energies_predation=1',
                ],
            },
        )
        self.validation = {
            'valid': True,
            'content_hash': hashlib.sha256(self.apl.content.encode()).hexdigest(),
            'revision': 'a' * 40, 'game_build': '12.0.1', 'diagnostics': [],
        }

    def _create(self, **kwargs):
        with patch('botend.services.simc_task_service.current_validation_identity',
                   return_value=('a' * 40, '12.0.1')):
            return create_execution(
                self.panel, requested_by=self.user_id, **kwargs,
            )

    def _run(self, task, sequence, status, key=None, label=None, dps=None,
             hero_talent_names=None):
        return SimulationRun.objects.create(
            task=task, sequence=sequence, candidate_key=key or f'candidate-{sequence}',
            candidate_label=label or key or f'Candidate {sequence}', status=status,
            result_summary={'dps': dps} if dps is not None else None,
            resource_manifest={'hero_talent_names': list(hero_talent_names or [])},
        )

    def _aggregate_candidate(self, key, dps, task_id, source_result_id, *, label=None,
                             candidate_type=None, icon_url='', source_label=''):
        return {
            'key': key, 'label': ('Test Trinket' if key in ('trinket', 'new-trinket') and label in ('Trinket', 'New Trinket')
                                  else label or key.title()),
            'type': candidate_type or ('base' if key == 'baseline' else 'gear_swap'),
            'icon_url': icon_url, 'source_label': source_label,
            'dps': dps, 'task_id': task_id, 'source_result_id': source_result_id,
            # Unknown evidence is explicit; populated effects are exercised by
            # test_incremental_results_overlay_the_active_variant_tooltip.
            'effects': [], 'effects_status': 'unknown',
        }

    def _published_success(self, hero_talent_names=None):
        # A completed immutable coordinate is intentionally not scheduled again.
        # Serializer-contract subtests need an independent fixture, so vary the
        # frozen scenario input after the first completed fixture in this database.
        completed = SimcBenchmarkExecution.objects.filter(
            panel=self.panel, results_finalized_at__isnull=False,
        ).count()
        if completed:
            SimcBenchmarkScenario.objects.filter(panel=self.panel, key='patchwerk').update(
                simulation_params={'iterations': 1000 + completed}
            )
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(
            task, 1, 'completed', 'baseline', dps=1234,
            hero_talent_names=hero_talent_names,
        )
        self._run(
            task, 2, 'completed', 'trinket', dps=1300,
            hero_talent_names=hero_talent_names,
        )
        reconcile_execution(execution)
        self.panel.is_public = True
        self.panel.save(update_fields=['is_public'])
        return execution

    def _replace_snapshot(self, execution, mutate, *, update_hash=True):
        snapshot = deepcopy(execution.config_snapshot)
        mutate(snapshot)
        values = {'config_snapshot': snapshot}
        if update_hash:
            values['config_hash'] = hashlib.sha256(json.dumps(
                snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest()
        SimcBenchmarkExecution.objects.filter(pk=execution.pk).update(**values)
