"""Reference-based SimC multi-run workflow contracts.

One request is one reference Task; candidate executions are immutable Runs.
Task rows never store a composed/frozen SimC body.
"""
import hashlib
import json

from django.contrib.auth.models import User
from unittest.mock import patch

from django.test import Client, RequestFactory, TestCase
from django.utils import timezone

from botend.models import (
    SimcApl,
    SimcContentTemplate,
    SimcProfile,
    SimcTalentString,
    SimcTask,
    SimulationRun,
)
from botend.services.simc_attribute_search import advance_attribute_search
from botend.services.simc_task_service import append_candidate_runs, create_task, initialize_task_runs
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.services.simc_composer import SimcComposer


TEST_VALIDATION_IDENTITY = ('test-simc-revision', 'test-game-build')


def mark_apl_valid(apl):
    values = {'validation_status': SimcApl.VALIDATION_VALID,
              'validated_content_hash': hashlib.sha256(apl.content.encode()).hexdigest(),
              'validation_revision': TEST_VALIDATION_IDENTITY[0],
              'validation_game_build': TEST_VALIDATION_IDENTITY[1], 'is_selectable': True}
    SimcApl.objects.filter(pk=apl.pk).update(**values)
    for key, value in values.items(): setattr(apl, key, value)


def setUpModule():
    from django.test import override_settings
    global _validation_settings, _validation_mock, _identity_mock
    _validation_settings = override_settings(SIMC_APL_CURRENT_IDENTITY=TEST_VALIDATION_IDENTITY)
    _validation_settings.enable()
    _identity_mock = patch(
        'botend.services.simc_task_service.current_validation_identity',
        return_value=TEST_VALIDATION_IDENTITY,
    )
    _identity_mock.start()
    _validation_mock = patch('botend.services.simc_task_service.validate_apl_for_profile', side_effect=lambda _p, apl: {
        'valid': True, 'content_hash': hashlib.sha256(apl.content.encode()).hexdigest(),
        'revision': TEST_VALIDATION_IDENTITY[0], 'game_build': TEST_VALIDATION_IDENTITY[1]})
    _validation_mock.start()


def tearDownModule():
    _validation_mock.stop(); _identity_mock.stop(); _validation_settings.disable()


class ReferenceBatchTaskCreationServiceTests(TestCase):
    def setUp(self):
        self.user_id = 2301
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id,
            name='Batch Fury Profile',
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batch"\nlevel=80\nspec=fury\ntalents=BASE\n'
                'head=,id=1001\nmain_hand=,id=2001'
            ),
            talent='BASE',
            gear_strength=5000,
            gear_crit=1000,
            gear_haste=2000,
            gear_mastery=3000,
            gear_versatility=4000,
            is_active=True,
        )
        self.talent = SimcTalentString.objects.create(
            owner_user_id=self.user_id, name='Batch Fury Talent', spec='fury', talent='BASE',
            is_active=True, is_selectable=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='Batch Base Template',
            spec='fury',
            content='{simulation_options}\n{player_config}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
            is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='Batch Fury APL',
            spec='fury',
            content='actions=/auto_attack',
            is_system=True,
            is_active=True,
            is_selectable=True,
        )
        mark_apl_valid(self.apl)

    def test_comparison_task_freezes_candidates_without_creating_runs(self):
        task = create_task(
            user_id=self.user_id,
            name='Reference comparison · helm',
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id, talent_string_id=self.talent.id,
            mode='comparison',
            simulation_params={
                'fight_style': 'Patchwerk',
                'max_time': 300,
                'desired_targets': 1,
            },
            mode_params={'request_manifest': {'kind': 'gear_candidates'}},
            candidates=[{
                'candidate_key': 'head-299001', 'candidate_label': 'head #299001',
                'candidate_params': {
                    'candidate_type': 'gear_swap', 'is_base': False,
                    'gear_swap': {'slot': 'head', 'raw_value': ',id=299001,ilevel=650',
                                  'item_id': 299001, 'source': 'bags'},
                    'untrusted_extra': 'drop-me',
                },
            }],
        )

        self.assertEqual(task.mode, 'comparison')
        self.assertEqual(task.profile_id, self.profile.id)
        self.assertEqual(task.template_id, self.template.id)
        self.assertEqual(task.apl_id, self.apl.id)
        self.assertIsNotNone(task.profile_version_id)
        self.assertIsNotNone(task.template_version_id)
        self.assertIsNotNone(task.apl_version_id)
        self.assertEqual(task.simulation_runs.count(), 0)
        frozen = task.mode_params['initial_candidates'][0]
        self.assertEqual(frozen['candidate_params']['candidate_type'], 'gear_swap')
        self.assertEqual(frozen['candidate_params']['gear_swap']['slot'], 'head')
        self.assertNotIn('untrusted_extra', frozen['candidate_params'])

    def test_attribute_sweep_task_freezes_candidates_without_creating_runs(self):
        task = create_task(
            user_id=self.user_id,
            name='Reference attributes · crit -50 / haste +50',
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id, talent_string_id=self.talent.id,
            mode='attribute_sweep',
            candidates=[{
                'candidate_key': 'crit-to-haste',
                'candidate_label': 'crit -50 / haste +50',
                'candidate_params': {
                    'candidate_type': 'attribute_ratings', 'is_base': False,
                    'attribute_ratings': {'crit': 950, 'haste': 2050,
                                          'mastery': 3000, 'versatility': 4000},
                    'search': {'round': 1, 'step': 50},
                },
            }],
        )

        self.assertEqual(task.mode, 'attribute_sweep')
        self.assertEqual(task.simulation_runs.count(), 0)
        frozen = task.mode_params['initial_candidates'][0]
        self.assertEqual(frozen['candidate_params']['attribute_ratings']['crit'], 950)
        self.assertEqual(frozen['candidate_params']['search'], {'round': 1, 'step': 50})

    def test_backend_processing_initializes_frozen_runs_then_executes_them(self):
        candidates = [{
            'candidate_key': 'base', 'candidate_label': '基准配置',
            'candidate_params': {'candidate_type': 'base', 'is_base': True},
        }, {
            'candidate_key': 'candidate-a', 'candidate_label': '候选 A',
            'candidate_params': {
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': {'slot': 'head', 'item_id': 299001, 'source': 'bags'},
            },
        }]
        task = create_task(
            user_id=self.user_id, name='backend-owned runs',
            profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id, talent_string_id=self.talent.id,
            mode='comparison', candidates=candidates,
        )
        self.assertEqual(task.simulation_runs.count(), 0)

        def complete_run(_task, run):
            run.status = 'completed'
            run.result_summary = {'dps': 1000 + run.sequence}
            run.save(update_fields=['status', 'result_summary'])
            return True

        monitor = SimcMonitor(None, task)
        with patch.object(monitor, 'process_reference_run', side_effect=complete_run) as execute:
            self.assertTrue(monitor.process_reference_task(task))

        runs = list(task.simulation_runs.order_by('sequence'))
        self.assertEqual([run.candidate_key for run in runs], ['base', 'candidate-a'])
        self.assertEqual(execute.call_count, 2)


class ReferenceBatchAPIViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reference_batch_api', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='API Batch Profile',
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222\n'
                '### Gear from Bags\nhead=,id=299001'
            ),
            talent='BASE',
            is_active=True,
        )
        self.talent = SimcTalentString.objects.create(
            owner_user_id=self.user.id, name='API Batch Fury Talent', spec='fury', talent='BASE',
            is_active=True, is_selectable=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='API Batch Base Template',
            spec='fury',
            content='{simulation_options}\n{player_config}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
            is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='API Batch Fury APL',
            spec='fury',
            content='actions=/auto_attack',
            is_system=True,
            is_active=True,
            is_selectable=True,
        )
        mark_apl_valid(self.apl)

    def test_gear_batch_api_creates_one_shared_reference_task_with_runs(self):
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'gear_candidates',
            'name': 'Reference gear batch',
            'simc_profile_id': self.profile.id,
            'candidates': [{'slot': 'head', 'item_id': 299001, 'source': 'bags'}],
            'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id, 'talent_string_id': self.talent.id,
        }), content_type='application/json')

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['task_id'])
        frozen = task.mode_params['initial_candidates']
        self.assertEqual(SimcTask.objects.count(), 1)
        self.assertEqual(task.simulation_runs.count(), 0)
        self.assertEqual(len(frozen), 2)
        self.assertEqual(task.mode, 'comparison')
        self.assertTrue(task.profile_id and task.template_id and task.apl_id)
        self.assertEqual(frozen[0]['candidate_params']['candidate_type'], 'base')
        self.assertEqual(frozen[1]['candidate_params']['candidate_type'], 'gear_swap')
        self.assertEqual(frozen[1]['candidate_params']['gear_swap']['item_id'], 299001)
        self.profile.refresh_from_db()
        self.assertIn('### Gear from Bags', self.profile.player_equipment)
        frozen_player_block = task.profile_version.payload['player_equipment']
        self.assertNotIn('### Gear from Bags', frozen_player_block)
        self.assertNotIn('id=299001', frozen_player_block)
        self.assertIn('head=,id=212048', frozen_player_block)
        self.assertFalse(hasattr(task, 'final_simc_content'))

    def test_manual_equipment_attribute_search_discovers_report_ratings_before_first_neighborhood(self):
        with patch(
            'botend.services.simc_task_service.current_validation_identity',
            return_value=TEST_VALIDATION_IDENTITY,
        ):
            response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
                'kind': 'attribute_variants',
                'name': 'Manual equipment attribute search',
                'simc_profile_id': self.profile.id,
                'attribute_step': 100,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id, 'talent_string_id': self.talent.id,
            }), content_type='application/json')

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['task_id'])
        frozen = task.mode_params['initial_candidates']
        self.assertEqual(len(frozen), 1)
        self.assertEqual(
            frozen[0]['candidate_params']['candidate_type'],
            'attribute_baseline_probe',
        )
        self.assertNotIn('attribute_ratings', frozen[0]['candidate_params'])

        started_at = timezone.now()
        task.current_status = 1
        task.started_at = started_at
        task.save(update_fields=['current_status', 'started_at'])
        initialize_task_runs(task, expected_started_at=started_at)
        probe = task.simulation_runs.get()
        report_html = '''
          <div class="player"><h2>Batcher: 100,000 dps</h2>
            <div class="player-section"><h3>Stats</h3><table class="sc">
              <tr><th></th><th>Raid-Buffed</th><th>Unbuffed</th><th>Gear Amount</th></tr>
              <tr><th>Crit</th><td>20.00% (1000)</td><td>20.00%</td><td>1,000</td></tr>
              <tr><th>Haste</th><td>30.00% (2000)</td><td>30.00%</td><td>2,000</td></tr>
              <tr><th>Mastery</th><td>40.00% (3000)</td><td>40.00%</td><td>3,000</td></tr>
              <tr><th>Versatility</th><td>10.00% (4000)</td><td>10.00%</td><td>4,000</td></tr>
            </table></div>
          </div>
        '''
        validation = SimcMonitor.validate_simulation_semantics(
            '''Player: Batcher warrior fury 90
  DPS=100000 DPS-Error=10/0.01%
  Actions:
    mortal_strike Count=50.0 pDPS=90000
''',
            report_html=report_html,
            extract_gear_ratings=True,
        )
        self.assertTrue(validation['valid'], validation)
        self.assertEqual(validation['gear_ratings'], {
            'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000,
        })
        probe.status = 'completed'
        probe.result_summary = validation
        probe.save(update_fields=['status', 'result_summary'])

        advanced = advance_attribute_search(task.id, expected_started_at=started_at)
        self.assertEqual(advanced['appended'], 12)
        runs = list(task.simulation_runs.order_by('sequence'))
        self.assertEqual(len(runs), 13)
        self.assertEqual(runs[0].candidate_key, 'round-1-baseline-probe')
        neighbors = [run.candidate_params['attribute_ratings'] for run in runs[1:]]
        self.assertEqual(len(neighbors), 12)
        self.assertTrue(all(sum(ratings.values()) == 10000 for ratings in neighbors))

    def test_attribute_continuation_reuses_exact_resource_versions_without_ext(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Attribute continuation profile',
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            talent='BASE',
            gear_strength=5000,
            gear_crit=1000,
            gear_haste=2000,
            gear_mastery=3000,
            gear_versatility=4000,
            is_active=True,
        )
        from botend.dashboard.api import SimcComparisonTaskAPIView
        api = SimcComparisonTaskAPIView()
        rows = api._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000},
            50,
        )
        candidates = []
        for index, (label, ratings, is_base, candidate) in enumerate(rows):
            candidates.append({
                'candidate_key': f'round-1-candidate-{index}',
                'candidate_label': label, 'round_number': 1,
                'candidate_params': {
                    'candidate_type': 'attribute_ratings',
                    'is_base': is_base, 'attribute_ratings': ratings, 'search': candidate,
                },
            })
        task = create_task(
            user_id=self.user.id, name='Attribute reference request', profile_id=profile.id,
            template_id=self.template.id, apl_id=self.apl.id, talent_string_id=self.talent.id, mode='attribute_sweep',
            simulation_params={'fight_style': 'Patchwerk', 'max_time': 300, 'desired_targets': 1},
            candidates=candidates,
        )
        source_profile_version_id = task.profile_version_id
        source_template_version_id = task.template_version_id
        source_apl_version_id = task.apl_version_id
        from botend.services.simc_task_service import initialize_task_runs
        initialize_task_runs(task)
        for index, run in enumerate(task.simulation_runs.order_by('sequence')):
            run.status = 'completed'
            run.result_summary = {'dps': [100000, 101500][index] if index < 2 else 100100}
            run.save(update_fields=['status', 'result_summary'])
        profile.player_equipment = profile.player_equipment.replace('id=212048', 'id=999999')
        profile.save(update_fields=['player_equipment'])
        self.template.content = '# changed after round one\n{player_config}\n{action_list}'
        self.template.save(update_fields=['content'])
        self.apl.content = 'actions=/changed_after_round_one'
        self.apl.save(update_fields=['content'])

        lease = timezone.now()
        task.current_status = 1
        task.started_at = lease
        task.save(update_fields=['current_status', 'started_at'])
        task.refresh_from_db()
        result = advance_attribute_search(task.id, expected_started_at=lease)
        next_runs = list(SimulationRun.objects.filter(id__in=result['run_ids']).order_by('sequence'))
        self.assertEqual(result['appended'], len(rows))
        self.assertEqual({run.task_id for run in next_runs}, {task.id})
        self.assertEqual({run.round_number for run in next_runs}, {2})
        task.refresh_from_db()
        self.assertEqual(task.profile_version_id, source_profile_version_id)
        self.assertEqual(task.template_version_id, source_template_version_id)
        self.assertEqual(task.apl_version_id, source_apl_version_id)
        self.assertEqual(task.simulation_runs.count(), len(rows) * 2)

        # Only the highest round gates continuation. A historical failed row must
        # neither poison the current round nor enter its DPS recommendation.
        historical = task.simulation_runs.filter(round_number=1).first()
        historical.status = 'failed'
        historical.save(update_fields=['status'])
        for index, run in enumerate(next_runs):
            run.status = 'completed'
            run.result_summary = {'dps': [100000, 101500][index] if index < 2 else 100100}
            run.save(update_fields=['status', 'result_summary'])
        third = advance_attribute_search(task.id, expected_started_at=lease)
        self.assertEqual(set(SimulationRun.objects.filter(id__in=third['run_ids']).values_list(
            'round_number', flat=True)), {3})

    def test_attribute_search_refines_100_to_50_to_20_before_converging(self):
        from botend.services.simc_attribute_search import attribute_variants

        ratings = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        rows = attribute_variants(ratings, step=100)
        candidates = [{
            'candidate_key': f'candidate-{index}', 'candidate_label': label,
            'round_number': 1,
            'candidate_params': {
                'candidate_type': 'attribute_ratings', 'is_base': is_base,
                'attribute_ratings': candidate_ratings, 'search': search,
            },
        } for index, (label, candidate_ratings, is_base, search) in enumerate(rows)]
        with patch(
            'botend.services.simc_task_service.current_validation_identity',
            return_value=TEST_VALIDATION_IDENTITY,
        ):
            task = create_task(
                user_id=self.user.id, name='progressive attribute refinement',
                profile_id=self.profile.id, template_id=self.template.id,
                apl_id=self.apl.id, talent_string_id=self.talent.id, mode='attribute_sweep', candidates=candidates,
            )
        lease = timezone.now()
        task.current_status = 1
        task.started_at = lease
        task.save(update_fields=['current_status', 'started_at'])
        initialize_task_runs(task, expected_started_at=lease)

        def complete_round(round_number):
            for run in task.simulation_runs.filter(round_number=round_number):
                run.status = 'completed'
                run.result_summary = {
                    'dps': 100000 if run.candidate_params.get('is_base') else 99900,
                    'dps_error': 10,
                }
                run.save(update_fields=['status', 'result_summary'])

        complete_round(1)
        coarse = advance_attribute_search(task.id, expected_started_at=lease)
        self.assertEqual(coarse['recommendation']['step'], 50)
        self.assertEqual({
            run.candidate_params['search']['step']
            for run in SimulationRun.objects.filter(id__in=coarse['run_ids'])
        }, {50})

        complete_round(2)
        medium = advance_attribute_search(task.id, expected_started_at=lease)
        self.assertEqual(medium['recommendation']['step'], 20)
        self.assertEqual({
            run.candidate_params['search']['step']
            for run in SimulationRun.objects.filter(id__in=medium['run_ids'])
        }, {20})

        complete_round(3)
        precise = advance_attribute_search(task.id, expected_started_at=lease)
        self.assertTrue(precise['converged'])
        self.assertEqual(precise['recommendation']['step'], 20)
        self.assertEqual(
            precise['recommendation']['stop_reason'],
            'local_optimum_20_pairwise',
        )
        self.assertEqual(precise['appended'], 12)
        marginal_runs = list(SimulationRun.objects.filter(
            id__in=precise['run_ids'],
        ).order_by('sequence'))
        self.assertEqual(
            [
                (
                    run.candidate_params['search']['marginal_gain']['stat'],
                    run.candidate_params['search']['marginal_gain']['amount'],
                )
                for run in marginal_runs
            ],
            [
                (stat, amount)
                for stat in ('crit', 'haste', 'mastery', 'versatility')
                for amount in (20, 50, 100)
            ],
        )
        weights = {'crit': 2, 'haste': 3, 'mastery': 1, 'versatility': 0.5}
        for run in marginal_runs:
            marginal = run.candidate_params['search']['marginal_gain']
            run.status = 'completed'
            run.result_summary = {
                'dps': 100000 + weights[marginal['stat']] * marginal['amount'],
                'dps_error': 5,
            }
            run.save(update_fields=['status', 'result_summary'])

        completed = advance_attribute_search(task.id, expected_started_at=lease)
        self.assertTrue(completed['converged'])
        self.assertEqual(completed['appended'], 0)
        task.refresh_from_db()
        persisted_gains = task.analysis_result['attribute_search']['marginal_gains']
        self.assertEqual(len(persisted_gains), 12)
        self.assertEqual(
            next(
                row for row in persisted_gains
                if row['stat'] == 'haste' and row['amount'] == 100
            )['dps_gain'],
            300,
        )
        from botend.dashboard.api import SimcRegularCompareAPIView
        report = SimcRegularCompareAPIView()._build_reference_attribute_report(
            task.simulation_runs.order_by('sequence'), task.analysis_result,
        )
        safe_report = SimcRegularCompareAPIView._safe_attribute_report(report)
        self.assertEqual(report['steps'], [100, 50, 20])
        self.assertEqual(len(report['marginal_gains']), 12)
        self.assertEqual(len(safe_report['marginal_gains']), 12)
        self.assertEqual(
            [point['step'] for point in report['search_path']],
            [100, 50, 20],
        )
        self.assertEqual(report['stop_reason'], 'local_optimum_20_pairwise')
        self.assertTrue(report['converged'])
        self.assertEqual(report['recommendation']['round'], 3)
        self.assertEqual(report['recommendation']['step'], 20)
        self.assertEqual(
            report['recommendation']['id'],
            task.simulation_runs.get(round_number=3, candidate_params__is_base=True).id,
        )

    def test_attribute_search_advances_when_gain_exceeds_independent_combined_error(self):
        from botend.dashboard.api import SimcComparisonTaskAPIView
        rows = SimcComparisonTaskAPIView()._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 50,
        )
        candidates = [{
            'candidate_key': f'candidate-{index}', 'candidate_label': label,
            'candidate_params': {
                'candidate_type': 'attribute_ratings', 'is_base': is_base,
                'attribute_ratings': ratings, 'search': candidate,
            },
        } for index, (label, ratings, is_base, candidate) in enumerate(rows)]
        with patch(
                'botend.services.simc_task_service.current_validation_identity',
                return_value=TEST_VALIDATION_IDENTITY):
            task = create_task(
                user_id=self.user.id, name='error-aware continuation', profile_id=self.profile.id,
                template_id=self.template.id, apl_id=self.apl.id, talent_string_id=self.talent.id,
                mode='attribute_sweep', candidates=candidates,
            )
        from botend.services.simc_task_service import initialize_task_runs
        runs = initialize_task_runs(task)
        for index, run in enumerate(runs):
            run.status = 'completed'
            run.result_summary = {
                'dps': 100384 if index == 1 else 100000,
                'dps_error': 268,
            }
            run.save(update_fields=['status', 'result_summary'])
        lease = timezone.now()
        task.current_status = 1
        task.started_at = lease
        task.save(update_fields=['current_status', 'started_at'])

        result = advance_attribute_search(task.id, expected_started_at=lease)

        self.assertFalse(result['converged'])
        self.assertEqual(result['appended'], len(rows))
        self.assertEqual(result['recommendation']['ratings'], rows[1][1])
        self.assertEqual(set(SimulationRun.objects.filter(
            id__in=result['run_ids'],
        ).values_list('round_number', flat=True)), {2})

    def test_attribute_continuation_requires_success_parseable_dps_and_consistent_current_versions(self):
        from botend.dashboard.api import SimcComparisonTaskAPIView
        api = SimcComparisonTaskAPIView()
        rows = api._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 50,
        )
        candidates = []
        for index, (label, ratings, is_base, candidate) in enumerate(rows):
            candidates.append({
                'candidate_key': f'candidate-{index}', 'candidate_label': label,
                'candidate_params': {'candidate_type': 'attribute_ratings', 'is_base': is_base,
                                     'attribute_ratings': ratings, 'search': candidate},
            })
        task = create_task(
            user_id=self.user.id, name='guarded', profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id, talent_string_id=self.talent.id,
            mode='attribute_sweep', candidates=candidates,
        )
        from botend.services.simc_task_service import initialize_task_runs
        runs = initialize_task_runs(task)
        for run in runs:
            run.status = 'completed'; run.result_summary = {'dps': 100000}
            run.save(update_fields=['status', 'result_summary'])
        lease = timezone.now()
        task.current_status = 1
        task.started_at = lease
        task.save(update_fields=['current_status', 'started_at'])

        runs[1].status = 'failed'; runs[1].save(update_fields=['status'])
        with self.assertRaisesRegex(ValueError, '全部成功'):
            advance_attribute_search(task.id, expected_started_at=lease)
        runs[1].status = 'completed'; runs[1].result_summary = {}
        runs[1].save(update_fields=['status', 'result_summary'])

        with patch('botend.dashboard.api.SimcRegularCompareAPIView._get_result_file_content', return_value='<html></html>'), \
                patch('botend.dashboard.api.SimcRegularCompareAPIView._parse_regular_result', return_value={}):
            with self.assertRaisesRegex(ValueError, 'DPS'):
                advance_attribute_search(task.id, expected_started_at=lease)

        task.profile_version_id = task.template_version_id
        task.save(update_fields=['profile_version'])
        with self.assertRaisesRegex(ValueError, '资源版本不一致'):
            advance_attribute_search(task.id, expected_started_at=lease)

    def test_complete_reference_task_put_only_renames_and_cannot_reset_status_or_inputs(self):
        task = create_task(
            user_id=self.user.id, name='Immutable run', profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id, talent_string_id=self.talent.id,
            simulation_params={'iterations': 100},
        )
        task.current_status = 2
        task.save(update_fields=['current_status'])

        response = self.client.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': 'Renamed only', 'current_status': 0,
            'simc_profile_id': 0, 'task_type': 2, 'ext': 'tampered',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task.refresh_from_db()
        self.assertEqual(task.name, 'Renamed only')
        self.assertEqual(task.current_status, 2)
        self.assertEqual(task.profile_id, self.profile.id)
        self.assertEqual(task.simulation_params, {'iterations': 100})

    def test_complete_reference_task_post_rerun_copies_frozen_execution(self):
        task = create_task(
            user_id=self.user.id, name='Immutable source', profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id, talent_string_id=self.talent.id,
            simulation_params={'iterations': 100},
        )
        task.current_status = 2
        task.save(update_fields=['current_status'])

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'id': task.id, 'action': 'rerun', 'talent_string_id': self.talent.id,
        }), content_type='application/json')

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        rerun = SimcTask.objects.get(id=payload['data']['id'])
        self.assertNotEqual(rerun.id, task.id)
        self.assertEqual(rerun.source_task_id, task.id)
        self.assertEqual(rerun.simulation_params, {'iterations': 100})
        self.assertEqual(rerun.profile_version_id, task.profile_version_id)
        self.assertEqual(rerun.template_version_id, task.template_version_id)
        self.assertEqual(rerun.apl_version_id, task.apl_version_id)
        task.refresh_from_db()
        self.assertEqual(task.simulation_params, {'iterations': 100})

    def test_task_preview_uses_reference_versions_and_never_ext_body(self):
        """Reference task detail exposes component refs/params, not frozen manifest text."""
        from botend.services.simc_task_service import create_task
        from botend.dashboard.api import SimcTaskPreviewAPIView

        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Preview Profile', spec='warrior_fury',
            player_config_mode='manual_equipment', player_equipment='warrior="Preview"',
            is_active=True,
        )
        template = SimcContentTemplate.objects.create(
            name='Preview Template', spec='warrior_fury',
            content='iterations=100', is_active=True, is_selectable=True,
        )
        apl = SimcApl.objects.create(
            name='Preview APL', spec='warrior_fury', content='actions=/auto_attack',
            is_active=True, is_selectable=True, owner_user_id=self.user.id,
        )
        mark_apl_valid(apl)
        talent = SimcTalentString.objects.create(
            owner_user_id=self.user.id, name='Preview Talent', spec='warrior_fury',
            talent='PREVIEW', is_active=True, is_selectable=True,
        )
        task = create_task(
            user_id=self.user.id, name='Reference preview', profile_id=profile.id,
            template_id=template.id, apl_id=apl.id, talent_string_id=talent.id, mode='normal',
            simulation_params={'iterations': 100}, mode_params={'candidate_type': 'base'},
        )
        request = RequestFactory().get(f'/api/simc-task/preview/?task_id={task.id}')
        request.user = self.user
        response = SimcTaskPreviewAPIView().get(request)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['profile_id'], profile.id)
        self.assertEqual(payload['data']['profile_version_id'], task.profile_version_id)
        self.assertEqual(payload['data']['template_version_id'], task.template_version_id)
        self.assertEqual(payload['data']['apl_version_id'], task.apl_version_id)
        self.assertEqual(payload['data']['simulation_params']['iterations'], 100)
        self.assertNotIn('content', payload['data'])

    def test_reference_task_rerun_is_exact_copy_without_mutating_source(self):
        """Task rerun copies frozen request and immutable versions exactly."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Rerun Profile', spec='warrior_fury',
            player_config_mode='manual_equipment', player_equipment='warrior="Rerun"', is_active=True,
        )
        template = SimcContentTemplate.objects.create(
            name='Rerun Template', spec='warrior_fury',
            content='iterations=100', is_active=True, is_selectable=True,
        )
        apl = SimcApl.objects.create(
            name='Rerun APL', spec='warrior_fury', content='actions=/auto_attack',
            is_active=True, is_selectable=True, owner_user_id=self.user.id,
        )
        mark_apl_valid(apl)
        talent = SimcTalentString.objects.create(
            owner_user_id=self.user.id, name='Rerun Talent', spec='warrior_fury',
            talent='RERUN', is_active=True, is_selectable=True,
        )
        source = create_task(
            user_id=self.user.id, name='Source', profile_id=profile.id,
            template_id=template.id, apl_id=apl.id, talent_string_id=talent.id, mode='normal',
            simulation_params={'iterations': 100}, mode_params={'candidate_type': 'base'},
        )
        source.current_status = 2
        source.save(update_fields=['current_status'])
        rerun = create_rerun(source.id, self.user.id)
        source.refresh_from_db()
        self.assertNotEqual(rerun.id, source.id)
        self.assertEqual(rerun.profile_version_id, source.profile_version_id)
        self.assertEqual(rerun.template_version_id, source.template_version_id)
        self.assertEqual(rerun.apl_version_id, source.apl_version_id)
        self.assertEqual(rerun.simulation_params['iterations'], 100)
        self.assertEqual(source.simulation_params['iterations'], 100)
class ReferenceBatchWorkerOverrideTests(TestCase):
    def test_candidate_composition_preserves_addon_omnium_metadata(self):
        """Saved Loadouts share Addon's required omnium metadata with the active build."""
        baseline = {
            'player_import_mode': 'manual_equipment',
            'player_equipment': (
                'warrior="Batcher"\nlevel=90\nspec=fury\n'
                'talents=BASE\n'
                'omnium_talents=136817:1/136819:1/136822:1\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            'talent': 'BASE',
        }
        request = SimcMonitor.apply_candidate_overrides(
            baseline, {
                'candidate_type': 'talent_override',
                'talent_override': 'CANDIDATE',
            }
        )
        composer = SimcComposer(2301)
        parsed = composer._parse_player_export(request['player_equipment'])
        self.assertEqual(
            parsed['talents'],
            'talents=CANDIDATE\nomnium_talents=136817:1/136819:1/136822:1',
        )
        self.assertIn(
            'omnium_talents=136817:1/136819:1/136822:1',
            request['player_equipment'],
        )

    def test_worker_applies_candidate_differences_to_runtime_request_only(self):
        baseline = {
            'player_equipment': (
                'warrior="Batcher"\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            'talent': 'BASE',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
        }

        gear_request = SimcMonitor.apply_candidate_overrides(baseline, {
            'candidate_type': 'gear_swap',
            'gear_swap': {'slot': 'head', 'raw_value': ',id=299001,ilevel=650'},
        })
        self.assertIn('head=,id=299001,ilevel=650', gear_request['player_equipment'])
        self.assertNotIn('head=,id=212048', gear_request['player_equipment'])

        talent_request = SimcMonitor.apply_candidate_overrides(baseline, {
            'candidate_type': 'talent_override',
            'talent_override': 'NEW_BUILD',
        })
        self.assertEqual(talent_request['talent'], 'NEW_BUILD')
        self.assertIn('talents=NEW_BUILD', talent_request['player_equipment'])
        self.assertNotIn('talents=BASE', talent_request['player_equipment'])

        attribute_request = SimcMonitor.apply_candidate_overrides(baseline, {
            'candidate_type': 'attribute_ratings',
            'attribute_ratings': {
                'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000,
            },
        })
        self.assertEqual(attribute_request['gear_crit'], 950)
        self.assertEqual(attribute_request['gear_haste'], 2050)
        self.assertEqual(baseline['gear_crit'], 1000)
