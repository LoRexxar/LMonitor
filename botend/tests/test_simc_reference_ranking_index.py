"""Write-side ranking projection; no SimC task/binary or HTTP reads."""
from copy import deepcopy
from io import StringIO
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from botend.models import (
    SimcApl, SimcBackendBinary, SimcContentTemplate, SimcProfile, SimcTalentString,
    SimcReferenceSimulation, SimcReferenceSimulationConfig, SimcReferenceSimulationSettings,
    SimcReferenceSimulationScenario, SimcReferencePublication,
)
from botend.services import simc_reference_simulations as source
from botend.services import simc_reference_ranking_index as index


class RankingIndexTests(TestCase):
    def setUp(self):
        index.register_signals()
        backend, _ = SimcBackendBinary.objects.update_or_create(identifier='production', defaults={'name': 'route'})
        template = SimcContentTemplate.objects.create(name='shared', spec='default', content='optimal_raid=0')
        self.settings, _ = SimcReferenceSimulationSettings.objects.update_or_create(use_ptr=False,
            defaults={'backend': backend, 'template': template, 'is_enabled': True})
        self.settings.scenarios.all().delete()
        self.config = SimcReferenceSimulationConfig.objects.create(spec_key='warrior_fury', settings=self.settings)
        self.scene = SimcReferenceSimulationScenario.objects.create(settings=self.settings, name='single',
            simulation_params={'desired_targets': 1, 'max_time': 300, 'iterations': 10000})
        self.profile = SimcProfile.objects.create(name='gear', spec='warrior_fury', class_name='warrior',
            player_config_mode='manual_equipment', player_equipment='warrior="test"\nspec=fury')
        self.apl = SimcApl.objects.create(name='apl', spec='warrior_fury', class_name='warrior', is_system=True, is_selectable=True, content='actions=/auto_attack')
        self.talent = SimcTalentString.objects.create(name='talent', spec='warrior_fury', talent='demo', is_system=True)
        self.publication = SimcReferencePublication.objects.create(spec_key='warrior_fury', is_published=True)
        self.publication.profiles.add(self.profile)
        self.publication.talents.add(self.talent)
        self.publication.apls.add(self.apl)

    def result(self, *, status='completed', dps=100, frozen=None):
        frozen = frozen or source.snapshot(self.config, self.profile, self.talent, self.apl, self.scene)
        return SimcReferenceSimulation.objects.create(config=self.config, scenario=self.scene, profile=self.profile,
            talent=self.talent, apl=self.apl, requested_by_id=1, status=status, dps=dps,
            snapshot=frozen, signature=source.tasks._compute_content_hash(frozen))

    def current(self):
        from botend.models import SimcReferenceRankingEntry
        return SimcReferenceRankingEntry.objects.filter(is_current=True)

    def test_latest_success_legacy_hash_idempotence_and_no_late_overwrite(self):
        frozen = source.snapshot(self.config, self.profile, self.talent, self.apl, self.scene)
        frozen['backend'] += ['old-sha', 'old-build']
        old = self.result(frozen=frozen)
        self.assertTrue(index.sync_result(old.pk))
        latest = self.result(dps=200)
        self.assertTrue(index.sync_result(latest.pk))
        self.assertFalse(index.sync_result(old.pk))
        failed = self.result(status='failed', dps=None)
        self.assertFalse(index.sync_result(failed.pk))
        self.assertEqual(self.current().get().result_id, latest.pk)
        with CaptureQueriesContext(connection) as queries:
            index.refresh_configs([self.config.pk])
            index.refresh_configs([self.config.pk])
        writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(writes, [])
        old.refresh_from_db()
        self.assertEqual(old.snapshot, frozen)
        call_command('refresh_simc_reference_rankings', '--config', str(self.config.pk), stdout=StringIO())
        self.assertEqual(self.current().count(), 1)
        ids = [self.config.pk]
        with self.captureOnCommitCallbacks(execute=True):
            index.invalidate_configs(ids)
            ids.clear()
            self.assertFalse(self.current().exists())
        self.assertEqual(self.current().get().result_id, latest.pk)
        with self.assertNumQueries(0), self.captureOnCommitCallbacks(execute=True) as callbacks:
            index.invalidate_configs([])
        self.assertEqual(callbacks, [])

    def test_resource_publication_and_scenario_changes_invalidate_and_restore(self):
        row = self.result()
        index.sync_result(row.pk)
        self.talent.talent = 'changed'
        with self.captureOnCommitCallbacks(execute=True):
            self.talent.save()
        self.assertFalse(self.current().exists())
        self.talent.talent = 'demo'
        with self.captureOnCommitCallbacks(execute=True):
            self.talent.save()
        self.assertEqual(self.current().get().result_id, row.pk)
        with self.captureOnCommitCallbacks(execute=True):
            self.profile.simcreferencepublication_set.clear()
        self.assertFalse(self.current().exists())
        with self.captureOnCommitCallbacks(execute=True):
            self.publication.profiles.add(self.profile)
        self.assertTrue(self.current().exists())
        with self.captureOnCommitCallbacks(execute=True):
            self.scene.is_enabled = False
            self.scene.save()
        self.assertFalse(self.current().exists())

    def test_full_input_and_scene_ownership_not_just_signature(self):
        good = self.result()
        wrong = deepcopy(good.snapshot)
        wrong['resources']['profile'][1] = '0' * 64
        self.assertFalse(index.sync_result(self.result(frozen=wrong).pk))
        index.sync_result(good.pk)
        duplicate = SimcReferenceSimulationScenario.objects.create(settings=self.settings, name='ambiguous',
            simulation_params=self.scene.simulation_params)
        index.refresh_configs([self.config.pk])
        self.assertFalse(self.current().exists())
        duplicate.delete()
        # Non-finite values cannot be persisted by the MySQL driver. Exercise
        # the real numerical guard before storage, then real ORM invalidation.
        good.dps = float('inf')
        self.assertIsNone(index._validate(good, self.config, {}, {}, {}))
        SimcReferenceSimulation.objects.filter(pk=good.pk).update(dps=0)
        index.refresh_configs([self.config.pk])
        self.assertFalse(self.current().exists())

    def test_comparison_identity_ignores_binary_metadata_but_not_full_conditions(self):
        params = source.scenario_simulation_params(self.scene)
        key = index.comparison_key(self.config, params)
        self.settings.backend.current_version = 'new-binary'
        self.settings.backend.game_build = 'new-build'
        self.assertEqual(key, index.comparison_key(self.config, params))
        self.assertNotEqual(key, index.comparison_key(self.config, dict(params, iterations=2000)))
        self.settings.template.content += '\noptimal_raid=1'
        self.assertNotEqual(key, index.comparison_key(self.config, params))
