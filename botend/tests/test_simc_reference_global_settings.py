"""Central settings regressions through real ORM and queue/public consumers."""
from django.test import TestCase

from botend.models import (SimcReferenceSimulationSettings, SimcReferenceSimulationConfig,
                           SimcReferenceSimulation, SimcProfile, SimcTalentString, SimcApl)
from botend.services import simc_reference_simulations as service
from botend.services.simc_reference_rankings import current_rankings
from botend.tests import test_simc_reference_simulations as simulation_tests
from botend.tests.simc_reference_test_fixtures import publish_resources


class GlobalReferenceSettingsTests(TestCase):
    setUp = simulation_tests.ReferenceSimulationTests.setUp
    enqueue = simulation_tests.ReferenceSimulationTests.enqueue
    dispatch = simulation_tests.ReferenceSimulationTests.dispatch
    complete = simulation_tests.ReferenceSimulationTests.complete
    results = simulation_tests.ReferenceSimulationTests.results

    def second_spec(self):
        apl = SimcApl.objects.create(name='Fire', spec='mage_fire', class_name='mage', is_system=True,
                                     is_selectable=True, content='actions=/fireball')
        talent = SimcTalentString.objects.create(name='Fire', spec='mage_fire', talent='fire', is_system=True, default_apl=apl)
        profile = SimcProfile.objects.create(name='Fire', spec='mage_fire', class_name='mage', player_config_mode='manual_equipment')
        publish_resources('mage_fire', profiles=[profile], talents=[talent], apls=[apl])
        service.initialize_configs()
        return SimcReferenceSimulationConfig.objects.get(spec_key='mage_fire'), profile, talent, apl

    def test_global_save_affects_two_specs_and_preserves_frozen_history(self):
        other, profile, talent, apl = self.second_spec()
        self.assertEqual(other.settings_id, self.config.settings_id)
        self.assertEqual(SimcReferenceSimulationSettings.objects.filter(use_ptr=False).count(), 1)
        service.enqueue_configs([self.config.pk, other.pk], self.user.pk)
        self.complete()
        frozen = list(SimcReferenceSimulation.objects.order_by('pk').values('pk', 'signature', 'snapshot', 'task_id'))
        self.assertEqual(len(self.results()), 2)
        settings = SimcReferenceSimulationSettings.objects.get(use_ptr=False)
        settings.duration = 240
        settings.save()
        self.assertEqual(SimcReferenceSimulation.objects.count(), 4)
        self.assertEqual(self.results(), [])
        self.assertEqual(service.public_results('mage_fire', profile, talent, apl), [])
        self.assertEqual(current_rankings(SimcReferenceSimulationConfig.objects.get(pk=self.config.pk)), {})
        self.assertEqual(list(SimcReferenceSimulation.objects.order_by('pk').values('pk', 'signature', 'snapshot', 'task_id')), frozen)
        self.assertEqual(service.enqueue_configs([self.config.pk, other.pk], self.user.pk)['created'], 4)
        self.assertEqual(set(SimcReferenceSimulation.objects.filter(status='queued').values_list('snapshot__simulation_params__max_time', flat=True)), {240})

    def test_queued_settings_drift_rejected_but_submitted_task_remains_frozen(self):
        self.enqueue()
        SimcReferenceSimulationSettings.objects.filter(use_ptr=False).update(duration=240)
        self.dispatch()
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='failed').count(), 2)
        self.enqueue()
        self.dispatch()
        tasks = list(SimcReferenceSimulation.objects.filter(status='submitted').values_list('task_id', flat=True))
        self.assertEqual(len(tasks), 2)
        SimcReferenceSimulationSettings.objects.filter(use_ptr=False).update(duration=180)
        self.complete()
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='completed').count(), 2)
        self.assertEqual(set(SimcReferenceSimulation.objects.filter(task_id__in=tasks).values_list('snapshot__simulation_params__max_time', flat=True)), {240})
        self.assertEqual(self.results(), [])

    def test_settings_drift_during_preparation_does_not_create_task(self):
        from unittest.mock import patch
        from botend.models import SimcTask
        self.enqueue()
        original = service.tasks.prepare_task_creation

        def change_settings(**kwargs):
            prepared = original(**kwargs)
            SimcReferenceSimulationSettings.objects.filter(use_ptr=False).update(iterations=2000)
            return prepared

        with patch.object(service.tasks, 'prepare_task_creation', side_effect=change_settings):
            self.dispatch()
        self.assertEqual(SimcTask.objects.count(), 0)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='failed').count(), 2)

    def test_removed_target_during_preparation_is_not_submitted(self):
        from unittest.mock import patch
        from botend.models import SimcTask
        self.enqueue()
        original = service.tasks.prepare_task_creation

        def change_targets(**kwargs):
            prepared = original(**kwargs)
            SimcReferenceSimulationSettings.objects.filter(use_ptr=False).update(targets=[5])
            return prepared

        with patch.object(service.tasks, 'prepare_task_creation', side_effect=change_targets):
            self.dispatch()
        removed = SimcReferenceSimulation.objects.get(snapshot__simulation_params__desired_targets=1)
        retained = SimcReferenceSimulation.objects.get(snapshot__simulation_params__desired_targets=5)
        self.assertEqual(removed.status, 'failed')
        self.assertIsNone(removed.task_id)
        self.assertEqual(retained.status, 'submitted')
        self.assertEqual(SimcTask.objects.count(), 1)
        self.assertEqual(retained.task.simulation_params['desired_targets'], 5)

    def test_only_universal_shared_templates_are_valid(self):
        settings = self.config.settings
        self.template.spec = 'warrior_fury'
        self.template.save()
        settings.template = self.template
        with self.assertRaisesRegex(ValueError, '全专精'):
            service.validate_settings(settings)

    def test_initialize_creates_only_thin_spec_references(self):
        other, *_ = self.second_spec()
        fields = {field.name for field in SimcReferenceSimulationConfig._meta.concrete_fields}
        self.assertEqual(fields, {'id', 'spec_key', 'use_ptr', 'settings'})
        self.assertEqual(other.settings_id, self.config.settings_id)
        with self.assertRaises(AttributeError):
            other.duration = 42
