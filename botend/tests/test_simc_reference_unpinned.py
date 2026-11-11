"""公开模拟仅绑定后端路由；旧三元组冻结输入和签名不可改写。"""

from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    SimcApl, SimcBackendBinary, SimcProfile, SimcReferenceSimulation,
    SimcTalentString, SimcTask, SimulationRun,
)
from botend.services import simc_reference_simulations as service
from botend.services.simc_reference_management import scheme_results
from botend.services.simc_reference_rankings import current_rankings, leaderboard, ranking_entry
from botend.tests import test_simc_reference_simulations as fixtures


class UnpinnedReferenceTests(TestCase):
    setUp = fixtures.ReferenceSimulationTests.setUp
    enqueue = fixtures.ReferenceSimulationTests.enqueue
    results = fixtures.ReferenceSimulationTests.results

    def legacy_rows(self):
        for row in SimcReferenceSimulation.objects.all():
            row.snapshot['backend'] = [self.backend.pk, 'old-binary-revision', 'old-game-build']
            row.signature = service.tasks._compute_content_hash(row.snapshot)
            row.save(update_fields=['snapshot', 'signature'])
        return {row.pk: (deepcopy(row.snapshot), row.signature)
                for row in SimcReferenceSimulation.objects.all()}

    def test_legacy_queued_dispatch_deduplicates_and_preserves_frozen_inputs(self):
        self.enqueue()
        SimcReferenceSimulation.objects.update(scenario=None)
        before = self.legacy_rows()
        SimcBackendBinary.objects.filter(pk=self.backend.pk).update(
            current_version='', game_build='new-build', local_worker_enabled=False,
        )
        self.assertEqual(self.enqueue(), {'created': 0, 'skipped': 2, 'errors': []})
        self.config.refresh_from_db()
        group = scheme_results(self.config, 1).object_list[0]
        self.assertTrue(group['has_active'])
        self.assertTrue(all(not scene['stale'] and scene['latest'] for scene in group['scenes']))
        with patch('botend.services.simc_task_service.current_validation_identity',
                   return_value=('current-agent-binary', 'current-agent-build')):
            service.maintain_simulations()
        self.assertEqual(SimcTask.objects.count(), 2)
        for row in SimcReferenceSimulation.objects.select_related('task'):
            self.assertEqual(row.status, 'submitted', row.error)
            self.assertEqual((row.snapshot, row.signature), before[row.pk])
            self.assertEqual(row.task.mode, 'normal')
            self.assertNotIn('reference_backend', row.task.mode_params.get('request_manifest', {}))
            actual = {kind: [getattr(row.task, f'{kind}_version').resource_id,
                             getattr(row.task, f'{kind}_version').content_hash]
                      for kind in ('profile', 'talent', 'apl', 'template')}
            self.assertEqual(actual, row.snapshot['resources'])
            self.assertEqual(row.task.simulation_params, row.snapshot['simulation_params'])
            SimcTask.objects.filter(pk=row.task_id).update(current_status=2)
            SimulationRun.objects.create(task_id=row.task_id, sequence=1, status='completed',
                                         result_summary={'dps': 1234.5, 'valid': True},
                                         resource_manifest={'backend_version': 'different-current-binary'})
        service.maintain_simulations()
        self.assertEqual(len(self.results()), 2)
        self.assertEqual(self.enqueue()['skipped'], 2)
        for row in SimcReferenceSimulation.objects.all():
            self.assertEqual((row.snapshot, row.signature), before[row.pk])
            self.assertEqual(row.status, 'completed')
        self.backend.refresh_from_db()
        self.assertFalse(self.backend.local_worker_enabled)

    def test_32_combinations_legacy_results_reused_by_all_projections(self):
        profiles = [self.profile] + [SimcProfile.objects.create(
            name=f'equipment-{i}', spec='warrior_fury', class_name='warrior',
            player_config_mode='manual_equipment', player_equipment=f'warrior="p{i}"\nspec=fury',
        ) for i in range(3)]
        talents = [self.talent] + [SimcTalentString.objects.create(
            name=f'talent-{i}', spec='warrior_fury', talent=f'talent-{i}', is_system=True,
        ) for i in range(3)]
        apls = [self.apl, SimcApl.objects.create(
            name='alternate-apl', spec='warrior_fury', class_name='warrior',
            is_system=True, is_selectable=True, content='actions=/bloodthirst', source='simc_upstream',
        )]
        self.publication.profiles.add(*profiles)
        self.publication.talents.add(*talents)
        self.publication.apls.add(*apls)
        self.assertEqual(self.enqueue()['created'], 64)
        before = self.legacy_rows()
        SimcReferenceSimulation.objects.update(status='completed', dps=1234.5)
        # This fixture uses a bulk completion, so explicitly maintain its read index.
        from botend.services.simc_reference_ranking_index import refresh_configs
        refresh_configs([self.config.pk])
        SimcBackendBinary.objects.filter(pk=self.backend.pk).update(
            current_version='new-binary', game_build='new-build', local_worker_enabled=False,
        )
        self.config.refresh_from_db()
        self.assertEqual(self.enqueue(), {'created': 0, 'skipped': 64, 'errors': []})
        self.assertEqual(self.enqueue(force=True)['created'], 64)
        # Pending reruns do not hide the last valid legacy result.
        for profile in profiles:
            for talent in talents:
                for apl in apls:
                    with self.subTest(profile=profile.pk, talent=talent.pk, apl=apl.pk):
                        results = service.public_results('warrior_fury', profile, talent, apl)
                        self.assertEqual(len(results), 2)
                        for result in results:
                            self.assertIn(result['signature'], {sig for _, sig in before.values()})
                            self.assertEqual(result['ranking']['overall_total'], 32)
                            self.assertEqual(result['ranking']['spec_total'], 32)
                            self.assertEqual(result['ranking']['overall_rank'], 1)
        config = type(self.config).objects.select_related('settings__backend', 'settings__template').get(pk=self.config.pk)
        with CaptureQueriesContext(connection) as queries:
            ranks = current_rankings(config)
        self.assertLessEqual(len(queries), 4)
        self.assertEqual(len(ranks), 64)
        for page_number in (1, 2):
            page = scheme_results(config, page_number)
            self.assertEqual(page.paginator.count, 32)
            for group in page:
                for scene in group['scenes']:
                    self.assertFalse(scene['stale'])
                    self.assertEqual(scene['result'].signature, before[scene['result'].pk][1])
                    self.assertEqual(scene['ranking']['overall_total'], 32)
        first_id = min(before)
        signature = before[first_id][1]
        board = leaderboard(config, signature, 'overall')
        self.assertEqual(len(board['entries']), 32)
        self.assertEqual(ranking_entry(config.pk), (config.pk, signature, 'overall'))
        for pk, frozen in before.items():
            row = SimcReferenceSimulation.objects.get(pk=pk)
            self.assertEqual((row.snapshot, row.signature), frozen)
        self.backend.refresh_from_db()
        self.assertFalse(self.backend.local_worker_enabled)

    def test_latest_success_is_unique_across_old_and_new_signatures(self):
        self.enqueue()
        self.legacy_rows()
        SimcReferenceSimulation.objects.update(status='completed', dps=100)
        old = SimcReferenceSimulation.objects.order_by('pk').first()
        scene = self.config.settings.scenarios.get(pk=old.scenario_id)
        frozen = service.snapshot(self.config, self.profile, self.talent, self.apl, scene)
        new = SimcReferenceSimulation.objects.create(
            config=self.config, scenario=scene, profile=self.profile, talent=self.talent, apl=self.apl,
            requested_by_id=self.user.pk, snapshot=frozen, signature=service.tasks._compute_content_hash(frozen),
            status='completed', dps=200,
        )
        from botend.services.simc_reference_ranking_index import refresh_configs
        refresh_configs([self.config.pk])
        results = self.results()
        self.assertEqual(results[0]['signature'], new.signature)
        self.assertEqual(results[0]['dps'], 200)
        self.assertEqual(results[0]['ranking']['overall_total'], 1)
        self.assertNotIn((self.config.pk, old.signature), current_rankings(self.config))
        self.assertIsNone(leaderboard(self.config, old.signature, 'overall'))

    def test_snapshot_normalization_only_removes_backend_version_fields(self):
        scene = self.config.settings.scenarios.first()
        frozen = service.snapshot(self.config, self.profile, self.talent, self.apl, scene)
        self.assertEqual(frozen['backend'], [self.backend.pk])
        legacy = deepcopy(frozen)
        legacy['backend'] += ['old-revision', 'old-build']
        normalized = service.normalized_snapshot(legacy)
        self.assertEqual(normalized, frozen)
        self.assertEqual(len(legacy['backend']), 3)
        for kind in ('profile', 'talent', 'apl', 'template'):
            altered = deepcopy(legacy)
            altered['resources'][kind][1] = 'changed-resource'
            self.assertNotEqual(service.normalized_snapshot(altered), frozen)
        altered = deepcopy(legacy)
        altered['simulation_params']['iterations'] += 1
        self.assertNotEqual(service.normalized_snapshot(altered), frozen)
