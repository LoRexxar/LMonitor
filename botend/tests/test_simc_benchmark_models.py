"""Contract tests for the SimC benchmark orchestration/reporting models."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.test import TestCase

from botend.models import (
    SimcApl,
    SimcBackendBinary,
    SimcBenchmarkCandidate,
    SimcBenchmarkCase,
    SimcBenchmarkExecution,
    SimcBenchmarkResult,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkScenario,
    SimcBenchmarkSpec,
    SimcContentTemplate,
    SimcProfile,
    SimcTask,
)


class SimcBenchmarkModelContractTests(TestCase):
    def setUp(self):
        self.backend = SimcBackendBinary.objects.create(
            identifier='benchmark-test', name='Benchmark test backend',
        )
        self.apl = SimcApl.objects.create(
            name='Benchmark APL', spec='warrior_fury', content='actions=/auto_attack',
        )
        self.template = SimcContentTemplate.objects.create(
            name='Benchmark template', spec='warrior_fury', content='iterations=1000',
        )
        self.profile = SimcProfile.objects.create(
            user_id=1, name='Benchmark profile', spec='warrior_fury',
        )
        self.panel = SimcBenchmarkPanel.objects.create(
            name='Weekly Fury', slug='weekly-fury', created_by_id=1,
        )
        self.panel_spec = SimcBenchmarkSpec.objects.create(
            panel=self.panel, class_name='warrior', spec_key='warrior_fury',
            label='Fury', apl=self.apl, template=self.template, backend=self.backend,
        )

    def test_models_expose_required_orchestration_and_report_fields(self):
        required = {
            SimcBenchmarkPanel: {
                'id', 'name', 'slug', 'description', 'created_by_id', 'is_active',
                'is_public', 'schedule_enabled', 'interval_seconds', 'next_run_at',
                'last_scheduled_at', 'published_execution', 'active_execution',
                'created_at', 'updated_at',
            },
            SimcBenchmarkSpec: {
                'id', 'panel', 'class_name', 'spec_key', 'label', 'apl', 'template',
                'backend', 'is_enabled', 'display_order',
            },
            SimcBenchmarkProfile: {
                'id', 'panel_spec', 'profile', 'label', 'is_enabled', 'display_order',
            },
            SimcBenchmarkScenario: {
                'id', 'panel', 'key', 'name', 'simulation_params', 'is_enabled',
                'display_order',
            },
            SimcBenchmarkCandidate: {
                'id', 'panel', 'key', 'label', 'candidate_type', 'params', 'spec_keys',
                'icon_url', 'source_label', 'is_enabled', 'display_order',
            },
            SimcBenchmarkExecution: {
                'id', 'panel', 'trigger', 'scheduled_slot', 'config_snapshot',
                'config_hash', 'status', 'result_hash', 'results_finalized_at',
                'created_at', 'completed_at',
            },
            SimcBenchmarkCase: {
                'id', 'execution', 'task', 'spec_key', 'scenario_key', 'profile_key',
                'spec_label', 'scenario_label', 'profile_label', 'coordinate_hash', 'status',
            },
            SimcBenchmarkResult: {
                'id', 'case', 'candidate_key', 'dps', 'created_at',
            },
        }
        forbidden_statistics = {'avg', 'average', 'median', 'p25', 'p75'}
        for model, fields in required.items():
            actual = {field.name for field in model._meta.fields}
            self.assertTrue(fields.issubset(actual))
            self.assertTrue(actual.isdisjoint(forbidden_statistics))

    def test_panel_interval_must_be_greater_than_zero(self):
        panel = SimcBenchmarkPanel(
            name='Invalid interval', slug='invalid-interval', created_by_id=1,
            interval_seconds=0,
        )
        with self.assertRaises(ValidationError) as context:
            panel.full_clean()
        self.assertIn('interval_seconds', context.exception.error_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkPanel.objects.create(
                    name='Invalid DB interval', slug='invalid-db-interval',
                    created_by_id=1, interval_seconds=0,
                )

    def test_trigger_constants_and_json_defaults(self):
        self.assertEqual(SimcBenchmarkExecution.TRIGGER_MANUAL, 'manual')
        self.assertEqual(SimcBenchmarkExecution.TRIGGER_SCHEDULE, 'schedule')
        self.assertEqual(
            {value for value, _label in SimcBenchmarkExecution.TRIGGER_CHOICES},
            {'manual', 'schedule'},
        )
        scenario = SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='single-target', name='Single target',
        )
        candidate = SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='baseline', label='Baseline', candidate_type='baseline',
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, trigger=SimcBenchmarkExecution.TRIGGER_MANUAL,
            config_hash='a' * 64,
        )
        self.assertEqual(scenario.simulation_params, {})
        self.assertEqual(candidate.params, {})
        self.assertEqual(candidate.spec_keys, [])
        self.assertEqual(execution.config_snapshot, {})

    def test_coordinate_uniqueness_contracts(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkSpec.objects.create(
                    panel=self.panel, class_name='warrior', spec_key='warrior_fury',
                    label='duplicate', apl=self.apl, template=self.template,
                    backend=self.backend,
                )

        SimcBenchmarkProfile.objects.create(
            panel_spec=self.panel_spec, profile=self.profile, label='Default',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkProfile.objects.create(
                    panel_spec=self.panel_spec, profile=self.profile, label='Duplicate',
                )

        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='patchwerk', name='Patchwerk',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkScenario.objects.create(
                    panel=self.panel, key='patchwerk', name='Duplicate',
                )

        SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='baseline', label='Baseline', candidate_type='baseline',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkCandidate.objects.create(
                    panel=self.panel, key='baseline', label='Duplicate',
                    candidate_type='baseline',
                )

    def test_case_coordinate_is_unique_by_three_business_keys(self):
        execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, trigger=SimcBenchmarkExecution.TRIGGER_MANUAL,
            config_hash='1' * 64,
        )
        first_task = SimcTask.objects.create(
            user_id=1, name='Coordinate task 1',
            simc_profile_id=self.profile.pk, backend=self.backend, mode='comparison',
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=first_task, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key=str(self.profile.pk),
            spec_label='Fury', scenario_label='Patchwerk', profile_label='Default',
            coordinate_hash='a' * 64,
        )
        second_task = SimcTask.objects.create(
            user_id=1, name='Coordinate task 2',
            simc_profile_id=self.profile.pk, backend=self.backend, mode='comparison',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkCase.objects.create(
                    execution=execution, task=second_task, spec_key='warrior_fury',
                    scenario_key='patchwerk', profile_key=str(self.profile.pk),
                    spec_label='Fury', scenario_label='Patchwerk',
                    profile_label='Default', coordinate_hash='b' * 64,
                )

        hash_collision_task = SimcTask.objects.create(
            user_id=1, name='Hash collision task',
            simc_profile_id=self.profile.pk, backend=self.backend, mode='comparison',
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=hash_collision_task, spec_key='warrior_fury',
            scenario_key='dungeon-slice', profile_key=str(self.profile.pk),
            spec_label='Fury', scenario_label='Dungeon Slice',
            profile_label='Default', coordinate_hash='a' * 64,
        )
        indexed_fields = {tuple(index.fields) for index in SimcBenchmarkCase._meta.indexes}
        self.assertIn(('coordinate_hash',), indexed_fields)

    def test_execution_slot_case_task_and_deletion_contracts(self):
        from django.utils import timezone

        slot = timezone.now()
        execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, trigger=SimcBenchmarkExecution.TRIGGER_SCHEDULE,
            scheduled_slot=slot, config_hash='b' * 64,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkExecution.objects.create(
                    panel=self.panel, trigger=SimcBenchmarkExecution.TRIGGER_SCHEDULE,
                    scheduled_slot=slot, config_hash='c' * 64,
                )

        for trigger, scheduled_slot in (
            (SimcBenchmarkExecution.TRIGGER_SCHEDULE, None),
            (SimcBenchmarkExecution.TRIGGER_MANUAL, slot),
        ):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    SimcBenchmarkExecution.objects.create(
                        panel=self.panel, trigger=trigger,
                        scheduled_slot=scheduled_slot, config_hash='e' * 64,
                    )

        task = SimcTask.objects.create(
            user_id=1, name='Benchmark task', simc_profile_id=self.profile.pk,
            backend=self.backend, mode='comparison',
        )
        case = SimcBenchmarkCase.objects.create(
            execution=execution, task=task, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key=str(self.profile.pk),
            spec_label='Fury', scenario_label='Patchwerk', profile_label='Default',
            coordinate_hash='d' * 64,
        )
        result = SimcBenchmarkResult.objects.create(
            case=case, candidate_key='baseline', dps=1234.5,
        )
        self.assertEqual(result.case.results.get().dps, 1234.5)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkResult.objects.create(
                    case=case, candidate_key='baseline', dps=1500,
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkResult.objects.create(
                    case=case, candidate_key='invalid', dps=0,
                )
        for field_name in ('spec_label', 'scenario_label', 'profile_label'):
            self.assertIsInstance(
                SimcBenchmarkCase._meta.get_field(field_name),
                models.CharField,
            )

        duplicate_task = SimcTask.objects.create(
            user_id=1, name='Duplicate coordinate task',
            simc_profile_id=self.profile.pk, backend=self.backend, mode='comparison',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcBenchmarkCase.objects.create(
                    execution=execution, task=duplicate_task,
                    spec_key='warrior_fury', scenario_key='patchwerk',
                    profile_key=str(self.profile.pk), spec_label='Fury',
                    scenario_label='Patchwerk', profile_label='Default',
                    coordinate_hash='e' * 64,
                )
        task_field = SimcBenchmarkCase._meta.get_field('task')
        self.assertIsInstance(task_field, models.OneToOneField)
        self.assertIs(task_field.remote_field.on_delete, models.SET_NULL)
        self.assertTrue(task_field.null)

        self.panel.published_execution = execution
        self.panel.save(update_fields=['published_execution'])
        panel_id = self.panel.pk
        self.panel.delete()
        self.assertFalse(SimcBenchmarkCase.objects.filter(execution__panel_id=panel_id).exists())
        self.assertTrue(SimcTask.objects.filter(pk=task.pk).exists())

    def test_case_clean_requires_comparison_task_mode(self):
        execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, trigger=SimcBenchmarkExecution.TRIGGER_MANUAL,
            config_hash='f' * 64,
        )
        task = SimcTask.objects.create(
            user_id=1, name='Wrong mode benchmark task',
            simc_profile_id=self.profile.pk, backend=self.backend, mode='normal',
        )
        case = SimcBenchmarkCase(
            execution=execution, task=task, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key=str(self.profile.pk),
            spec_label='Fury', scenario_label='Patchwerk', profile_label='Default',
            coordinate_hash='a' * 64,
        )
        with self.assertRaisesMessage(ValidationError, 'comparison') as context:
            case.full_clean()
        self.assertIn('task', context.exception.error_dict)

        task.mode = 'comparison'
        task.save(update_fields=['mode'])
        case.full_clean()

    def test_case_clean_reports_missing_task_as_validation_error(self):
        execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, trigger=SimcBenchmarkExecution.TRIGGER_MANUAL,
            config_hash='9' * 64,
        )
        missing_task_id = SimcTask.objects.order_by('-pk').values_list('pk', flat=True).first()
        missing_task_id = (missing_task_id or 0) + 1
        case = SimcBenchmarkCase(
            execution=execution, task_id=missing_task_id, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key=str(self.profile.pk),
            spec_label='Fury', scenario_label='Patchwerk', profile_label='Default',
            coordinate_hash='9' * 64,
        )

        with self.assertRaises(ValidationError) as context:
            case.full_clean()
        self.assertIn('task', context.exception.error_dict)

    def test_case_clean_reports_malformed_task_id_as_validation_error(self):
        execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, trigger=SimcBenchmarkExecution.TRIGGER_MANUAL,
            config_hash='8' * 64,
        )
        case = SimcBenchmarkCase(
            execution=execution, task_id='not-an-int', spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key=str(self.profile.pk),
            spec_label='Fury', scenario_label='Patchwerk', profile_label='Default',
            coordinate_hash='8' * 64,
        )

        with self.assertRaises(ValidationError) as context:
            case.full_clean()
        self.assertIn('task', context.exception.error_dict)

    def test_published_execution_cross_table_rules_belong_to_publish_service(self):
        """The model cannot constrain same-panel/completed publication across tables."""
        other_panel = SimcBenchmarkPanel.objects.create(
            name='Other panel', slug='other-panel', created_by_id=1,
        )
        incomplete_execution = SimcBenchmarkExecution.objects.create(
            panel=other_panel, trigger=SimcBenchmarkExecution.TRIGGER_MANUAL,
            config_hash='0' * 64,
        )
        self.panel.published_execution = incomplete_execution
        self.panel.full_clean()

    def test_resource_references_are_protected_and_panel_due_index_exists(self):
        for model, field_name in (
            (SimcBenchmarkSpec, 'apl'),
            (SimcBenchmarkSpec, 'template'),
            (SimcBenchmarkSpec, 'backend'),
            (SimcBenchmarkProfile, 'profile'),
        ):
            self.assertIs(
                model._meta.get_field(field_name).remote_field.on_delete,
                models.PROTECT,
            )
        self.assertIs(
            SimcBenchmarkPanel._meta.get_field('published_execution').remote_field.on_delete,
            models.SET_NULL,
        )
        indexed_fields = {tuple(index.fields) for index in SimcBenchmarkPanel._meta.indexes}
        self.assertIn(
            ('schedule_enabled', 'is_active', 'next_run_at'),
            indexed_fields,
        )
