import importlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management.base import CommandError
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from botend.dashboard.api import SimcAplCandidatesAPIView, SimcComparisonTaskAPIView, SimcProfileAPIView, SimcRegularCompareAPIView, SimcTaskAPIView, SimcSpecOptionsAPIView
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.management.commands.update_simc_binary import Command as UpdateSimcBinaryCommand
from botend.services.simc_player_config import build_player_config_detail, parse_manual_player_config, parse_manual_simc_candidates, parse_simc_player_profile
from botend.services.simc_composer import SimcComposer
from botend.services.simc_task_service import append_candidate_runs
from botend.models import DashboardUserGroup, DashboardUserGroupMembership, PlayerSpecTopPlayer, SeasonMeta, SimcApl, SimcAplSymbol, SimcBackendBinary, SimcContentTemplate, SimcProfile, SimcResourceVersion, SimcTalentString, SimcTask, SimcTaskArtifact, SimulationRun, WowItemSnapshot, WowTalentVersion
from botend.tests.simc_apl_symbol_test_utils import get_or_create_symbol_scope


TEST_SIMC_REVISION = 'a' * 40
TEST_WOW_BUILD = 'test-build'


class SimcDispatchSwitchTests(TestCase):
    def test_disabled_local_worker_does_not_scan_or_fail_pending_tasks(self):
        SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': 'Production', 'simc_path': '/tmp/simc',
                'local_worker_enabled': False,
            },
        )
        monitor = SimcMonitor(None, None)
        with (
            patch.object(monitor, 'ensure_local_simc_backend_current') as ensure_current,
            patch.object(monitor, 'fail_pending_tasks') as fail_pending,
        ):
            self.assertTrue(monitor.scan())
        ensure_current.assert_not_called()
        fail_pending.assert_not_called()


def get_test_backend():
    backend, _ = SimcBackendBinary.objects.update_or_create(
        identifier='production',
        defaults={
            'name': '正式服', 'platform': 'linux64',
            'simc_path': '/opt/simc', 'current_version': TEST_SIMC_REVISION,
            'is_active': True,
        },
    )
    get_or_create_symbol_scope(
        simc_revision=TEST_SIMC_REVISION,
        wow_build=TEST_WOW_BUILD,
        token='auto_attack',
        symbol_kind=SimcAplSymbol.KIND_ACTION,
        defaults={'is_active': True},
    )
    return backend


def create_test_task(**kwargs):
    kwargs.setdefault('backend', get_test_backend())
    return SimcTask.objects.create(**kwargs)


class SimcAplCanonicalClassAliasTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='apl_alias_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            name='Generic base template',
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='default',
            class_name='',
            content='{player_identity}\n{action_list}',
            is_active=True,
            is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='Default Unholy APL',
            spec='deathknight_unholy',
            class_name='deathknight',
            content='actions=/auto_attack',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            owner_user_id=None,
            is_active=True,
            is_selectable=True,
        )

    def test_apl_candidates_resolves_death_knight_alias_to_canonical_class(self):
        response = self.client.get(
            '/api/simc-apl-candidates/',
            {'spec': 'unholy', 'class_name': 'death_knight'},
        )

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['default_apl_id'], self.apl.id)
        self.assertEqual(payload['default_template_id'], self.base_template.id)
        self.assertEqual([row['id'] for row in payload['data']], [self.apl.id])


class SimcWorkerTaskRunLifecycleTests(TestCase):
    """The former group lifecycle coverage now belongs to one Task and its Runs."""

    def setUp(self):
        self.monitor = SimcMonitor(None, None)
        self.backend = get_test_backend()
        self.task = create_test_task(
            user_id=801, name='worker lifecycle', simc_profile_id=0,
            mode='comparison', current_status=0, is_active=True, backend=self.backend,
        )

    def _run(self, sequence, status='pending', dps=None, label=None):
        return SimulationRun.objects.create(
            task=self.task, sequence=sequence, candidate_key=f'candidate-{sequence}',
            candidate_label=label or f'candidate {sequence}', status=status,
            result_summary=None if dps is None else {'dps': dps},
        )

    def test_cancelled_task_fences_stale_local_run_writes(self):
        run = self._run(1, status='running')
        claimed_at = timezone.now()
        SimcTask.objects.filter(pk=self.task.pk).update(
            current_status=1,
            execution_owner=SimcTask.EXECUTION_OWNER_LOCAL,
            started_at=claimed_at,
        )
        self.monitor._active_claim_task_id = self.task.pk
        self.monitor._active_claim_started_at = claimed_at

        SimcTask.objects.filter(pk=self.task.pk).update(
            current_status=3,
            execution_owner=SimcTask.EXECUTION_OWNER_UNASSIGNED,
        )
        SimulationRun.objects.filter(pk=run.pk).update(status='cancelled')

        self.assertFalse(self.monitor._save_run_for_active_claim(
            self.task,
            run,
            ('running',),
            status='failed',
            error_detail='late local failure',
            completed_at=timezone.now(),
        ))
        self.assertFalse(self.monitor._save_run_for_active_claim(
            self.task,
            run,
            ('running',),
            status='completed',
            result_summary={'dps': 999999},
            completed_at=timezone.now(),
        ))
        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        self.assertIsNone(run.result_summary)
        self.assertNotEqual(run.error_detail, 'late local failure')

    def test_pending_run_keeps_task_nonterminal(self):
        self._run(1, 'completed', 100)
        self._run(2, 'running')
        self.assertFalse(self.monitor.process_reference_task(self.task))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_status, 0)
        self.assertIsNone(self.task.completed_at)

    def test_completed_runs_aggregate_into_task(self):
        self._run(1, 'completed', 100, 'baseline')
        self._run(2, 'completed', 110, 'candidate')
        self.assertTrue(self.monitor.process_reference_task(self.task))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_status, 2)
        self.assertEqual(self.task.analysis_result['succeeded'], 2)
        self.assertEqual(len(self.task.analysis_result['candidates']), 2)
        self.assertEqual(json.loads(self.task.result_summary)['runs'], [{'dps': 100}, {'dps': 110}])

    def test_partial_candidate_failure_does_not_discard_success(self):
        self._run(1, 'completed', 100)
        self._run(2, 'failed', label='broken').error_detail = 'bad candidate'
        SimulationRun.objects.filter(task=self.task, sequence=2).update(error_detail='bad candidate')
        self.assertTrue(self.monitor.process_reference_task(self.task))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_status, 2)
        self.assertEqual(self.task.analysis_result['failed'], 1)
        self.assertEqual(self.task.analysis_result['failed_candidates'][0]['candidate_label'], 'broken')

    def test_all_failed_runs_fail_task(self):
        self._run(1, 'failed')
        self._run(2, 'failed')
        self.assertFalse(self.monitor.process_reference_task(self.task))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_status, 3)
        self.assertIsNotNone(self.task.completed_at)

    def test_pending_runs_are_drained_independently(self):
        first = self._run(1)
        second = self._run(2)
        observed = []
        def execute(task, run):
            observed.append((task.current_status, run.id))
            run.status = 'completed' if run.id == first.id else 'failed'
            run.result_summary = {'dps': 100} if run.id == first.id else None
            run.save(update_fields=['status', 'result_summary'])
        with patch.object(self.monitor, 'process_reference_run', side_effect=execute):
            self.assertTrue(self.monitor.process_reference_task(self.task))
        self.assertEqual(observed, [(1, first.id), (1, second.id)])

    def test_draining_runs_persists_progress_after_each_terminal_run(self):
        self._run(1)
        self._run(2)
        observed_progress = []

        def execute(task, run):
            task.refresh_from_db()
            observed_progress.append(json.loads(task.ext or '{}').get('progress'))
            run.status = 'completed'
            run.result_summary = {'dps': 100 + run.sequence}
            run.save(update_fields=['status', 'result_summary'])

        with patch.object(self.monitor, 'process_reference_run', side_effect=execute):
            self.assertTrue(self.monitor.process_reference_task(self.task))

        self.assertEqual(observed_progress, [None, 50])

    def test_task_delete_cascades_runs_without_touching_other_task(self):
        own = self._run(1)
        other = create_test_task(
            user_id=802, name='other', simc_profile_id=0,
            mode='comparison', backend=self.backend,
        )
        foreign = SimulationRun.objects.create(task=other, sequence=1, status='pending')
        self.task.delete()
        self.assertFalse(SimulationRun.objects.filter(id=own.id).exists())
        self.assertTrue(SimulationRun.objects.filter(id=foreign.id).exists())

    def test_appending_pending_run_reopens_terminal_task(self):
        self.task.current_status = 3
        self.task.started_at = timezone.now()
        self.task.completed_at = timezone.now()
        self.task.error_detail = 'old failure'
        self.task.save(update_fields=['current_status', 'started_at', 'completed_at', 'error_detail'])

        append_candidate_runs(self.task, [{
            'candidate_key': 'round-2', 'candidate_label': 'round 2',
        }], round_number=2)

        self.task.refresh_from_db()
        run = self.task.simulation_runs.get()
        self.assertEqual(self.task.current_status, 0)
        self.assertIsNone(self.task.started_at)
        self.assertIsNone(self.task.completed_at)
        self.assertIsNone(self.task.error_detail)
        self.assertEqual(run.status, 'pending')
        self.assertEqual(run.round_number, 2)

    def test_reaggregating_terminal_runs_keeps_completed_at_stable(self):
        self._run(1, 'failed')
        self.assertFalse(self.monitor.process_reference_task(self.task))
        self.task.refresh_from_db()
        completed_at = self.task.completed_at

        self.assertFalse(self.monitor.process_reference_task(self.task))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_status, 3)
        self.assertEqual(self.task.completed_at, completed_at)

    def test_legacy_status_four_is_reconciled_from_run_facts(self):
        self.task.current_status = 4
        self.task.save(update_fields=['current_status'])
        self._run(1, 'completed', 100)

        self.assertTrue(self.monitor.process_reference_task(self.task))

        self.task.refresh_from_db()
        self.assertEqual(self.task.current_status, 2)
        self.assertEqual(json.loads(self.task.result_summary)['dps'], 100)

    def test_runs_from_inactive_other_task_do_not_affect_aggregation(self):
        self._run(1, 'completed', 100)
        other = create_test_task(
            user_id=802, name='inactive other', simc_profile_id=0,
            mode='comparison', current_status=0, is_active=False, backend=self.backend,
        )
        SimulationRun.objects.create(
            task=other, sequence=1, candidate_key='foreign', status='pending',
        )

        self.assertTrue(self.monitor.process_reference_task(self.task))

        self.task.refresh_from_db()
        self.assertEqual(self.task.current_status, 2)
        self.assertEqual(self.task.analysis_result['total'], 1)

    def test_legacy_batch_metadata_cannot_reassign_run_ownership(self):
        self.task.mode_params = {'legacy_batch_id': 999, 'batch_id': 802}
        self.task.save(update_fields=['mode_params'])
        run = self._run(1, 'completed', 100)
        other = create_test_task(
            user_id=802, name='forged target', simc_profile_id=0,
            mode='comparison', backend=self.backend,
        )

        self.assertTrue(self.monitor.process_reference_task(self.task))

        run.refresh_from_db()
        self.assertEqual(run.task_id, self.task.id)
        self.assertNotEqual(run.task_id, other.id)



class SimcTalentStringSaveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='talent_string_user', password='pwd')
        self.client.force_login(self.user)

    @patch('botend.dashboard.api.TalentBuildCodeService.build_api_view')
    def test_create_rejects_talent_string_without_resolved_hero_tree(self, build_api_view):
        build_api_view.return_value = {'talent_render_model': {'trees': []}}

        response = self.client.post('/api/simc-talent-string/', data=json.dumps({
            'name': '无法解析英雄树',
            'spec': 'warrior_fury',
            'talent': 'TEST_BUILD_CODE',
        }), content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertIn('无法获取英雄天赋树', response.json()['error'])
        self.assertFalse(SimcTalentString.objects.exists())

    @patch('botend.dashboard.api.TalentBuildCodeService.build_api_view')
    def test_update_rejects_talent_string_when_hero_tree_resolution_fails(self, build_api_view):
        row = SimcTalentString.objects.create(
            name='原天赋', spec='warrior_fury', talent='ORIGINAL_CODE',
            owner_user_id=self.user.id, is_active=True, is_selectable=True,
        )
        build_api_view.side_effect = RuntimeError('talent parser unavailable')

        response = self.client.put('/api/simc-talent-string/', data=json.dumps({
            'id': row.id,
            'name': '新天赋',
            'spec': 'warrior_fury',
            'talent': 'BROKEN_CODE',
        }), content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('无法获取英雄天赋树', response.json()['error'])
        row.refresh_from_db()
        self.assertEqual(row.name, '原天赋')
        self.assertEqual(row.talent, 'ORIGINAL_CODE')


class SimcProfileResourceListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='profile_resource_user', password='pwd')
        self.other_user = User.objects.create_user(username='profile_resource_other', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_profile_api_exposes_active_retail_and_ptr_talent_simulator_versions(self):
        WowTalentVersion.objects.create(
            key='retail-profile-link', branch='retail', major_version='12.0.7',
            label='正式服', is_active=True, is_default_simulator=True,
            status='active',
        )
        WowTalentVersion.objects.create(
            key='ptr-profile-link', branch='ptr', major_version='12.1.0',
            label='PTR', is_active=True, status='active',
        )
        response = self.client.get('/api/simc-profile/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['talent_versions'], {
            'retail': 'retail-profile-link',
            'ptr': 'ptr-profile-link',
        })

    def test_profile_list_exposes_authoritative_spec_icon_and_class_color(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='冰霜死亡骑士',
            class_name='deathknight',
            spec='deathknight_frost',
            player_config_mode='manual_equipment',
            player_equipment='deathknight="Frost"\nspec=frost\nhead=,id=1',
        )

        response = self.client.get('/api/simc-profile/')

        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json()['data'] if item['id'] == profile.id)
        self.assertIn('spell_deathknight_frostpresence.jpg', row['spec_icon_url'])
        self.assertEqual(row['class_color'], '#C41F3B')

    def test_profile_list_exposes_one_canonical_spec_for_all_persisted_identity_shapes(self):
        profiles = [
            SimcProfile.objects.create(
                user_id=self.user.id, name='短键冰法', class_name='mage_frost', spec='frost',
                player_config_mode='manual_equipment', player_equipment='mage="Frost"\nspec=frost',
            ),
            SimcProfile.objects.create(
                user_id=self.user.id, name='职业加短键冰法', class_name='mage', spec='frost',
                player_config_mode='manual_equipment', player_equipment='mage="Frost"\nspec=frost',
            ),
            SimcProfile.objects.create(
                user_id=self.user.id, name='完整键冰法', class_name='mage', spec='mage_frost',
                player_config_mode='manual_equipment', player_equipment='mage="Frost"\nspec=frost',
            ),
        ]

        response = self.client.get('/api/simc-profile/')

        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.json()['data']}
        for profile in profiles:
            self.assertEqual(rows[profile.id]['canonical_spec'], 'mage_frost')
            self.assertEqual(rows[profile.id]['spec_label'], '冰霜')
            self.assertIn('spell_frost_frostbolt02.jpg', rows[profile.id]['spec_icon_url'])

    def test_profile_save_persists_one_canonical_class_spec_identity(self):
        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': 'Canonical frost mage',
                'class_name': 'mage',
                'spec': 'frost',
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'mage="Tester"\\nhead=,id=1',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile = SimcProfile.objects.get(pk=response.json()['data']['id'])
        self.assertEqual(profile.class_name, 'mage')
        self.assertEqual(profile.spec, 'mage_frost')
        row = next(
            item for item in self.client.get('/api/simc-profile/').json()['data']
            if item['id'] == profile.id
        )
        self.assertEqual(row['canonical_spec'], 'mage_frost')

    def test_profile_list_neutrally_rejects_conflicting_class_and_full_spec(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='冲突标签', class_name='mage', spec='warrior_fury',
        )
        row = next(
            item for item in self.client.get('/api/simc-profile/').json()['data']
            if item['id'] == profile.id
        )
        self.assertEqual(row['canonical_spec'], '')
        self.assertEqual(row['class_color'], '#94A3B8')

    def test_profile_list_disambiguates_shared_short_specs_from_class_identity(self):
        identities = [
            ('deathknight_frost', 'frost', 'deathknight_frost'),
            ('druid_restoration', 'restoration', 'druid_restoration'),
            ('shaman_restoration', 'restoration', 'shaman_restoration'),
            ('paladin_holy', 'holy', 'paladin_holy'),
            ('priest_holy', 'holy', 'priest_holy'),
            ('paladin_protection', 'protection', 'paladin_protection'),
            ('warrior_protection', 'protection', 'warrior_protection'),
        ]
        profiles = [
            (SimcProfile.objects.create(
                user_id=self.user.id, name=canonical, class_name=class_name, spec=short_spec,
                player_config_mode='manual_equipment', player_equipment='warrior="Identity"',
            ), canonical)
            for class_name, short_spec, canonical in identities
        ]

        response = self.client.get('/api/simc-profile/')

        rows = {row['id']: row for row in response.json()['data']}
        self.assertEqual(
            {rows[profile.id]['canonical_spec'] for profile, _canonical in profiles},
            {canonical for _profile, canonical in profiles},
        )

    def test_list_exposes_migrated_system_profiles_as_read_only_resources(self):
        own = SimcProfile.objects.create(
            user_id=self.user.id, name='我的狂暴配置', spec='fury',
            player_config_mode='manual_equipment', player_equipment='warrior="Mine"\nhead=,id=1',
        )
        system = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', class_name='warrior',
            name='MID1 Fury player', spec='warrior_fury', sync_version='revision-1',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Default"\nspec=fury\nhead=,id=2',
        )
        foreign = SimcProfile.objects.create(
            user_id=self.other_user.id, name='其他用户配置', spec='fury',
            player_config_mode='manual_equipment', player_equipment='warrior="Other"',
        )
        inactive_system = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:inactive', name='停用系统配置', spec='mage_fire',
            player_config_mode='manual_equipment', player_equipment='mage="Inactive"',
            is_active=False,
        )
        unrelated_global = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_USER,
            name='非上游全局配置', spec='mage_fire',
            player_config_mode='manual_equipment', player_equipment='mage="Global"',
        )

        response = self.client.get('/api/simc-profile/')

        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.json()['data']}
        self.assertEqual(set(rows), {own.id, system.id})
        self.assertNotIn(foreign.id, rows)
        self.assertNotIn(inactive_system.id, rows)
        self.assertNotIn(unrelated_global.id, rows)
        self.assertFalse(rows[own.id]['is_system'])
        self.assertTrue(rows[own.id]['can_edit'])
        self.assertTrue(rows[own.id]['can_delete'])
        self.assertTrue(rows[system.id]['is_system'])
        self.assertFalse(rows[system.id]['can_edit'])
        self.assertFalse(rows[system.id]['can_delete'])
        self.assertEqual(rows[system.id]['source'], SimcProfile.SOURCE_SIMC_UPSTREAM)
        self.assertEqual(rows[system.id]['version'], '12.0')
        self.assertEqual(rows[system.id]['sync_version'], 'revision-1')
        self.assertEqual(rows[system.id]['equipment_line_count'], 3)

    def test_staff_product_admin_list_includes_inactive_and_other_owned_profiles_with_status(self):
        admin = User.objects.create_user(
            username='profile_resource_admin', password='pwd', is_staff=True,
        )
        self.client.force_login(admin)
        inactive_system = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury_inactive', class_name='warrior',
            name='12.1 PTR Fury', spec='warrior_fury', version='12.1', is_active=False,
        )
        other = SimcProfile.objects.create(
            user_id=self.other_user.id, name='其他用户配置', spec='fury', is_active=True,
        )

        response = self.client.get('/api/simc-profile/')

        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.json()['data']}
        self.assertIn(inactive_system.id, rows)
        self.assertIn(other.id, rows)
        self.assertFalse(rows[inactive_system.id]['is_active'])
        self.assertTrue(rows[inactive_system.id]['can_edit'])
        self.assertFalse(rows[inactive_system.id]['can_delete'])
        self.assertTrue(rows[other.id]['is_active'])

    def test_create_profile_without_attribute_overrides_keeps_them_absent(self):
        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': '不覆盖属性',
                'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Tester"\\nhead=,id=1',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'], response.content)
        profile = SimcProfile.objects.get(pk=response.json()['data']['id'])
        self.assertIsNone(profile.gear_strength)
        self.assertIsNone(profile.gear_crit)
        self.assertIsNone(profile.gear_haste)
        self.assertIsNone(profile.gear_mastery)
        self.assertIsNone(profile.gear_versatility)

        listed = {row['id']: row for row in self.client.get('/api/simc-profile/').json()['data']}
        self.assertIsNone(listed[profile.id]['gear_strength'])
        self.assertIsNone(listed[profile.id]['gear_crit'])

    def test_manual_equipment_persists_explicit_stat_overrides(self):
        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': '手动装备绿字覆盖',
                'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Tester"\\nhead=,id=1',
                'gear_strength': 93330,
                'gear_crit': 10730,
                'gear_haste': 18641,
                'gear_mastery': 21785,
                'gear_versatility': 6757,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        profile = SimcProfile.objects.get(pk=response.json()['data']['id'])
        self.assertEqual(profile.gear_strength, 93330)
        self.assertEqual(profile.gear_crit, 10730)
        self.assertEqual(profile.gear_haste, 18641)
        self.assertEqual(profile.gear_mastery, 21785)
        self.assertEqual(profile.gear_versatility, 6757)

    def test_profile_names_do_not_need_to_be_unique(self):
        duplicate_name = '允许重名 Profile'
        SimcProfile.objects.create(
            user_id=self.user.id,
            name=duplicate_name,
            class_name='mage',
            spec='mage_frost',
            player_config_mode='manual_equipment',
            player_equipment='mage="Existing"\nspec=frost\nhead=,id=1',
        )
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='待改名 Profile',
            class_name='warrior',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Before"\nspec=fury\nhead=,id=2',
        )

        update_response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id,
                'name': duplicate_name,
                'spec': profile.spec,
                'player_config_mode': profile.player_config_mode,
                'player_equipment': 'warrior="After"\nspec=fury\nhead=,id=3',
            }),
            content_type='application/json',
        )
        create_response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': duplicate_name,
                'spec': 'warrior_fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Created"\nspec=fury\nhead=,id=4',
            }),
            content_type='application/json',
        )

        self.assertEqual(update_response.status_code, 200, update_response.content)
        self.assertTrue(update_response.json()['success'], update_response.content)
        self.assertEqual(create_response.status_code, 200, create_response.content)
        self.assertTrue(create_response.json()['success'], create_response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.name, duplicate_name)
        self.assertIn('warrior="After"', profile.player_equipment)
        self.assertEqual(
            SimcProfile.objects.filter(user_id=self.user.id, name=duplicate_name).count(),
            3,
        )

    def test_switching_profile_source_preserves_omitted_overrides(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='属性转手动装备',
            spec='fury',
            player_config_mode='attribute_only',
            player_equipment='warrior="Tester"\\nhead=,id=1',
            talent='BUILD',
            gear_strength=0,
            gear_crit=10730,
            gear_haste=18641,
            gear_mastery=21785,
            gear_versatility=6757,
        )

        response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id,
                'name': profile.name,
                'spec': profile.spec,
                'player_config_mode': 'manual_equipment',
                'player_equipment': profile.player_equipment,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'], response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.gear_strength, 0)
        self.assertEqual(profile.gear_crit, 10730)
        self.assertEqual(profile.gear_haste, 18641)
        self.assertEqual(profile.gear_mastery, 21785)
        self.assertEqual(profile.gear_versatility, 6757)
        self.assertEqual(profile.talent, 'BUILD')

        clear_response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id,
                'talent': '',
                'gear_strength': None,
            }),
            content_type='application/json',
        )
        self.assertEqual(clear_response.status_code, 200, clear_response.content)
        self.assertTrue(clear_response.json()['success'], clear_response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.talent, '')
        self.assertIsNone(profile.gear_strength)
        self.assertEqual(profile.gear_crit, 10730)

    def test_every_mode_persists_explicit_zero_override(self):
        numeric = SimcProfileAPIView._profile_numeric_values(
            {'gear_strength': 0},
        )
        self.assertEqual(numeric['gear_strength'], 0)
        self.assertIsNone(numeric['gear_crit'])

    def test_equipment_update_preserves_explicit_overrides(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='装备快速更新清理',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Tester"\nlevel=90\nspec=fury\nhead=,id=1,ilevel=100',
            gear_strength=93330,
            gear_crit=10730,
            gear_haste=18641,
            gear_mastery=21785,
            gear_versatility=6757,
        )

        response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id,
                'equipment': [{'slot': 'head', 'item_id': 2, 'item_level': 200}],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertIn('head=,id=2,ilevel=200', profile.player_equipment)
        self.assertEqual(profile.gear_strength, 93330)
        self.assertEqual(profile.gear_crit, 10730)
        self.assertEqual(profile.gear_haste, 18641)
        self.assertEqual(profile.gear_mastery, 21785)
        self.assertEqual(profile.gear_versatility, 6757)

    def test_copying_manual_profile_preserves_explicit_overrides(self):
        source = SimcProfile.objects.create(
            user_id=self.user.id,
            name='历史污染手动配置',
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Tester"\\nhead=,id=1',
            gear_strength=93330,
            gear_crit=10730,
            gear_haste=18641,
            gear_mastery=21785,
            gear_versatility=6757,
        )

        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({'name': '手动配置副本', 'copy_from_id': source.id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        copied = SimcProfile.objects.get(pk=response.json()['data']['id'])
        self.assertEqual(copied.gear_strength, 93330)
        self.assertEqual(copied.gear_crit, 10730)
        self.assertEqual(copied.gear_haste, 18641)
        self.assertEqual(copied.gear_mastery, 21785)
        self.assertEqual(copied.gear_versatility, 6757)

    def test_copying_attribute_only_profile_preserves_null_and_zero(self):
        source = SimcProfile.objects.create(
            user_id=self.user.id,
            name='属性配置',
            spec='fury',
            player_config_mode='attribute_only',
            player_equipment=(
                'warrior="Tester"\nlevel=90\nspec=fury\nhead=,id=1\nneck=,id=2\nshoulders=,id=3\n'
                'back=,id=4\nchest=,id=5\nwrists=,id=6\nhands=,id=7\n'
                'waist=,id=8\nlegs=,id=9\nfeet=,id=10\nfinger1=,id=11\n'
                'finger2=,id=12\ntrinket1=,id=13\ntrinket2=,id=14\nmain_hand=,id=15'
            ),
            gear_strength=0,
            gear_crit=None,
            gear_haste=333,
            gear_mastery=444,
            gear_versatility=555,
        )

        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({'name': '属性配置副本', 'copy_from_id': source.id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        copied = SimcProfile.objects.get(pk=response.json()['data']['id'])
        self.assertEqual(copied.gear_strength, 0)
        self.assertIsNone(copied.gear_crit)
        self.assertEqual(copied.gear_haste, 333)
        self.assertEqual(copied.gear_mastery, 444)
        self.assertEqual(copied.gear_versatility, 555)

    def test_one_click_copy_generates_unique_copy_name_and_preserves_profile_semantics(self):
        source = SimcProfile.objects.create(
            user_id=self.user.id,
            name='PTR 狂暴战',
            source=SimcProfile.SOURCE_WCL,
            class_name='warrior',
            version='12.1',
            use_ptr=True,
            sync_version='wcl-revision-7',
            spec='warrior_fury',
            player_config_mode='wcl',
            battlenet_region='eu',
            battlenet_realm='Tarren Mill',
            battlenet_character='Tester',
            player_equipment='warrior="Tester"\nspec=fury\ntalents=BUILD\nhead=,id=1',
            talent='BUILD',
            is_active=True,
        )

        first_response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({'copy_from_id': source.id}),
            content_type='application/json',
        )
        second_response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({'copy_from_id': source.id}),
            content_type='application/json',
        )

        self.assertEqual(first_response.status_code, 200, first_response.content)
        self.assertEqual(second_response.status_code, 200, second_response.content)
        first = SimcProfile.objects.get(pk=first_response.json()['data']['id'])
        second = SimcProfile.objects.get(pk=second_response.json()['data']['id'])
        self.assertEqual(first.name, 'PTR 狂暴战 副本')
        self.assertEqual(second.name, 'PTR 狂暴战 副本 2')
        self.assertEqual(first.user_id, self.user.id)
        self.assertIsNone(first.system_key)
        for field in (
            'source', 'class_name', 'version', 'use_ptr', 'sync_version', 'spec',
            'player_config_mode', 'battlenet_region', 'battlenet_realm',
            'battlenet_character', 'player_equipment', 'talent', 'is_active',
        ):
            self.assertEqual(getattr(first, field), getattr(source, field), field)

    def test_staff_can_one_click_copy_visible_global_profile_into_private_copy(self):
        admin = User.objects.create_user(
            username='profile_copy_admin', password='pwd', is_staff=True,
        )
        self.client.force_login(admin)
        source = SimcProfile.objects.create(
            user_id=None,
            name='全局 WCL 奥法',
            source=SimcProfile.SOURCE_WCL,
            class_name='mage',
            version='12.1',
            use_ptr=True,
            sync_version='wcl-global-1',
            spec='mage_arcane',
            player_config_mode='wcl',
            player_equipment='mage="Tester"\nspec=arcane\ntalents=BUILD',
            talent='BUILD',
            is_active=False,
        )

        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({'copy_from_id': source.id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        copied = SimcProfile.objects.get(pk=response.json()['data']['id'])
        self.assertEqual(copied.user_id, admin.id)
        self.assertEqual(copied.name, '全局 WCL 奥法 副本')
        self.assertEqual(copied.source, SimcProfile.SOURCE_WCL)
        self.assertEqual(copied.player_config_mode, 'wcl')
        self.assertFalse(copied.is_active)

    def test_regular_user_cannot_copy_another_users_private_profile(self):
        another_user = User.objects.create_user(username='another-profile-owner', password='testpass')
        source = SimcProfile.objects.create(
            user_id=another_user.id,
            name='Private Profile',
            spec='mage_fire',
            player_config_mode='manual_equipment',
            player_equipment='mage="Private"',
            talent='private-build',
            is_active=True,
        )

        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({'copy_from_id': source.id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()['success'])
        self.assertFalse(SimcProfile.objects.filter(user_id=self.user.id, name='Private Profile 副本').exists())

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_battlenet_snapshot_stats_are_not_saved_as_explicit_overrides(self, preflight):
        preflight.return_value = {
            'simc_ready': True,
            'warnings': [],
            'simc_config': {
                'player_config_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Snapshotter',
                'spec': 'fury',
                'talent': 'BUILD',
                'player_equipment': 'warrior="Snapshotter"\nspec=fury\nhead=,id=1',
                'gear_strength': 5000,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
            },
        }

        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': 'Battle.net 不覆盖属性',
                'spec': 'fury',
                'player_config_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Snapshotter',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        profile = SimcProfile.objects.get(pk=response.json()['data']['id'])
        self.assertEqual(profile.player_equipment, 'warrior="Snapshotter"\nspec=fury\nhead=,id=1')
        self.assertIsNone(profile.gear_strength)
        self.assertIsNone(profile.gear_crit)
        self.assertIsNone(profile.gear_haste)
        self.assertIsNone(profile.gear_mastery)
        self.assertIsNone(profile.gear_versatility)

    def test_profile_api_persists_and_serializes_explicit_ptr_attribute(self):
        create = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': 'PTR 配置', 'spec': 'fury', 'use_ptr': True,
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Tester"\\nspec=fury\\nhead=,id=1',
            }),
            content_type='application/json',
        )
        self.assertTrue(create.json()['success'], create.content)
        profile = SimcProfile.objects.get(pk=create.json()['data']['id'])
        self.assertIs(profile.use_ptr, True)

        listed = {row['id']: row for row in self.client.get('/api/simc-profile/').json()['data']}
        self.assertIs(listed[profile.id]['use_ptr'], True)

        update = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id, 'name': profile.name, 'use_ptr': False,
                'spec': profile.spec, 'player_config_mode': profile.player_config_mode,
                'player_equipment': profile.player_equipment,
            }),
            content_type='application/json',
        )
        self.assertTrue(update.json()['success'], update.content)
        profile.refresh_from_db()
        self.assertIs(profile.use_ptr, False)

    def test_profile_api_can_switch_ptr_export_to_attribute_only(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='12.1 PTR大秘境天赋-属性强制版',
            spec='warrior_fury',
            class_name='warrior',
            use_ptr=True,
            player_config_mode='manual_equipment',
            talent='CURRENT_BUILD',
            player_equipment=(
                'warrior=PTR_Tester\n'
                'PtR = 1\n'
                'DeFaUlT_AcTiOnS = 1\n'
                'level=90\n'
                'race=human\n'
                'spec=fury\n'
                'talents=CURRENT_BUILD\n'
                'head=,id=1\n'
                'main_hand=,id=2'
            ),
            is_active=True,
        )

        response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id,
                'name': profile.name,
                'spec': profile.spec,
                'use_ptr': True,
                'player_config_mode': 'attribute_only',
                'player_equipment': profile.player_equipment,
                'talent': 'CURRENT_BUILD',
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'], response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.player_config_mode, 'attribute_only')
        self.assertIs(profile.use_ptr, True)
        self.assertEqual(profile.gear_crit, 1000)
        self.assertFalse(any(
            '=' in line and line.partition('=')[0].strip().lower() == 'ptr'
            for line in profile.player_equipment.splitlines()
        ))
        self.assertNotIn('default_actions', profile.player_equipment.lower())

        ptr_content = SimcComposer(self.user.id).compose_validation_input(
            profile, 'actions=/auto_attack',
        )
        self.assertEqual([
            line for line in ptr_content.splitlines()
            if '=' in line and line.partition('=')[0].strip().lower() == 'ptr'
        ], ['ptr=1'])
        self.assertNotIn('default_actions', ptr_content.lower())

        profile.use_ptr = False
        live_content = SimcComposer(self.user.id).compose_validation_input(
            profile, 'actions=/auto_attack',
        )
        self.assertFalse(any(
            '=' in line and line.partition('=')[0].strip().lower() == 'ptr'
            for line in live_content.splitlines()
        ))

    def test_profile_api_rejects_non_boolean_ptr_attribute(self):
        response = self.client.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': '错误 PTR 配置', 'spec': 'fury', 'use_ptr': 'true',
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Tester"\\nspec=fury\\nhead=,id=1',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertIn('use_ptr', response.json()['error'])

    def test_regular_user_cannot_mutate_upstream_profile(self):
        system = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:mage_frost', class_name='mage',
            name='MID1 Frost player', spec='mage_frost',
            player_config_mode='manual_equipment', player_equipment='mage="Default"',
        )

        update = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({'id': system.id, 'name': '被篡改'}),
            content_type='application/json',
        )
        delete = self.client.delete(
            '/api/simc-profile/',
            data=json.dumps({'id': system.id}),
            content_type='application/json',
        )

        self.assertFalse(update.json()['success'])
        self.assertFalse(delete.json()['success'])
        system.refresh_from_db()
        self.assertEqual(system.name, 'MID1 Frost player')
        self.assertTrue(system.is_active)

    def test_admin_can_edit_inactive_upstream_profile_equipment(self):
        admin = User.objects.create_user(
            username='profile_default_editor', password='pwd', is_superuser=True,
        )
        self.client.force_login(admin)
        system = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:mage_frost', class_name='mage',
            name='12.1 Frost player', spec='mage_frost', version='12.1', is_active=False,
            player_config_mode='manual_equipment',
            player_equipment='mage="Default"\\nspec=frost\\nhead=,id=100,ilevel=276\\n',
        )

        # 管理列表允许编辑的默认/未生效 Profile，编辑表单读取接口也必须
        # 使用同一管理员可见范围，不能把它错误判成“找不到配置”。
        edit_load = self.client.get(f'/api/simc-profile/{system.id}/')
        detail = self.client.get(f'/api/simc-player-config-detail/?profile_id={system.id}')
        update = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({'id': system.id, 'equipment': [
                {'slot': 'head', 'item_id': 999, 'item_level': 300},
            ]}),
            content_type='application/json',
        )

        self.assertEqual(edit_load.status_code, 200)
        self.assertTrue(edit_load.json()['success'], edit_load.content)
        self.assertEqual(edit_load.json()['id'], system.id)
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()['data']['profile']['can_edit'])
        self.assertEqual(update.status_code, 200)
        self.assertTrue(update.json()['success'], update.content)
        system.refresh_from_db()
        self.assertIn('head=,id=999,ilevel=300', system.player_equipment)
        self.assertEqual(system.source, SimcProfile.SOURCE_SIMC_UPSTREAM)
        self.assertFalse(system.is_active)

    def test_owner_can_update_equipment_ids_and_levels_without_replacing_other_profile_lines(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='可编辑装备', spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Tester"\\nspec=fury\\ntalents=BUILD\\nhead=,id=100,ilevel=276\\nchest=,id=200,ilevel=276\\n',
        )
        response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({'id': profile.id, 'equipment': [
                {'slot': 'head', 'item_id': 999, 'item_level': 300},
                {'slot': 'chest', 'item_id': 888, 'item_level': 285},
            ]}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.content)
        profile.refresh_from_db()
        self.assertIn('warrior="Tester"', profile.player_equipment)
        self.assertIn('talents=BUILD', profile.player_equipment)
        self.assertIn('head=,id=999,ilevel=300', profile.player_equipment)
        self.assertIn('chest=,id=888,ilevel=285', profile.player_equipment)

    def test_workbench_uses_api_edit_permission_and_shows_sync_metadata(self):
        source = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text()

        self.assertIn('row.is_system', source)
        self.assertIn('row.can_edit', source)
        self.assertIn('row.can_delete', source)
        self.assertIn('系统默认配置', source)
        self.assertNotIn('系统只读', source)
        self.assertIn('版本 ${row.version}', source)
        self.assertIn('row.sync_version', source)
        self.assertIn('row.equipment_line_count', source)
        self.assertIn('simcProfileMatchesSpecFilter', source)
        self.assertIn("String(row.canonical_spec || '')", source)
        self.assertNotIn("frost_death_knight: ['deathknight', 'frost']", source)
        self.assertNotIn("frost_mage: ['mage', 'frost']", source)
        self.assertIn('data-profile-equipment-slot', source)
        self.assertIn('item_id', source)
        self.assertIn('item_level', source)
        self.assertIn("method: 'PUT'", source)
        self.assertIn("body: JSON.stringify({ id:", source)
        self.assertIn("equipment })", source)


class SimcTemplateAPIViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='template_user', password='pwd', is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_list_returns_metadata_preview_without_apl_source(self):
        """The legacy template list must not mix full APL source into its base-template view."""
        base = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='fury', name='基础模板', content='warrior="Template"', is_active=True,
        )
        apl = SimcApl.objects.create(
            name='默认 APL',
            spec='warrior_fury',
            content='actions+=/bloodthirst',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
        )
        response = self.client.get('/api/simc-template/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual([row['id'] for row in payload['templates']], [base.id])
        self.assertEqual(payload['templates'][0]['template_type'], 'base_template')
        self.assertNotIn('template_content', payload['templates'][0])
        self.assertNotIn('content', payload['templates'][0])
        self.assertTrue(SimcApl.objects.filter(id=apl.id).exists())
        self.assertTrue(SimcContentTemplate.objects.filter(id=base.id).exists())

    def test_default_player_cannot_create_or_mutate_identity_fields(self):
        """default_player 不允许通过 API 创建或改变 template_type/source/spec 身份字段。"""
        protected = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            spec='warrior_fury', class_name='warrior', name='protected',
            player_config_mode='manual_equipment', player_equipment='secret baseline',
            is_active=True,
        )
        forbidden_attempts = [
            self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
                'content': 'changed',
            }), content_type='application/json'),
            self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
                'content': 'changed', 'source': 'user',
            }), content_type='application/json'),
            self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
                'content': 'changed', 'spec': 'warrior_arms',
            }), content_type='application/json'),
            self.client.post('/api/simc-template/', data=json.dumps({
                'content': 'forged',
                'source': 'simc_upstream', 'spec': 'warrior_fury',
            }), content_type='application/json'),
        ]
        for response in forbidden_attempts:
            self.assertIn(response.status_code, (403, 404, 405), response.content)
            self.assertFalse(response.json()['success'])
        protected.refresh_from_db()
        self.assertEqual(protected.player_equipment, 'secret baseline')
        self.assertEqual(protected.source, SimcProfile.SOURCE_SIMC_UPSTREAM)
        self.assertEqual(protected.spec, 'warrior_fury')

    def test_default_player_upstream_content_and_metadata_are_read_only(self):
        """simc_upstream 的 default_player 对 staff 也完全只读。"""
        protected = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            spec='warrior_fury', class_name='warrior', name='Baseline',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Fury"\nlevel=80', is_active=True,
        )
        response = self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
            'content': 'warrior="Fury"\nlevel=80\nrace=orc',
            'name': 'Updated Baseline',
            'is_selectable': True,
            'is_active': False,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()['success'])
        protected.refresh_from_db()
        self.assertNotIn('race=orc', protected.player_equipment)
        self.assertEqual(protected.name, 'Baseline')
        self.assertTrue(protected.is_active)

    def test_base_template_rejects_actor_lines(self):
        """base_template 必须恰好一个 {player_config} 占位符，不允许 actor= 行。"""
        valid = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='', name='Valid', content='fight_style=Patchwerk\n{player_config}\n',
            is_active=True,
        )
        response = self.client.put(f'/api/simc-template/?id={valid.id}', data=json.dumps({
            'content': 'fight_style=Patchwerk\nactor="Bad"\n{player_config}\n',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('actor', response.json()['error'].lower())

        response = self.client.put(f'/api/simc-template/?id={valid.id}', data=json.dumps({
            'content': 'fight_style=Patchwerk\n{player_config}\n{player_config}\n',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('player_config', response.json()['error'].lower())

        response = self.client.put(f'/api/simc-template/?id={valid.id}', data=json.dumps({
            'content': 'fight_style=Patchwerk\nmax_time=300\n',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('player_config', response.json()['error'].lower())


    def test_delete_rejects_default_player(self):
        """DELETE 不允许删除 default_player 类型。"""
        protected = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            spec='warrior_fury', class_name='warrior', name='Baseline',
            player_config_mode='manual_equipment', player_equipment='warrior="Fury"',
            is_active=True,
        )
        response = self.client.delete(f'/api/simc-template/?id={protected.id}')
        self.assertEqual(response.status_code, 405)
        self.assertFalse(response.json()['success'])
        self.assertTrue(SimcProfile.objects.filter(id=protected.id).exists())

    def test_delete_allows_user_content(self):
        """DELETE 允许删除用户创建的 base_template/custom_apl/default_apl。"""
        user_base = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='', name='My Base', content='fight_style=Patchwerk\n{player_config}',
        )
        user_apl = SimcApl.objects.create(
            name='My APL',
            spec='warrior_fury',
            content='actions=/auto_attack',
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
        )
        response = self.client.delete(f'/api/simc-template/?id={user_base.id}')
        self.assertEqual(response.status_code, 405)
        self.assertFalse(response.json()['success'])
        self.assertTrue(SimcContentTemplate.objects.filter(id=user_base.id).exists())

        response = self.client.delete(f'/api/simc-workbench/apls/{user_apl.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SimcApl.objects.filter(id=user_apl.id).exists())
    def test_non_staff_cannot_mutate_system_template(self):
        user = User.objects.create_user(username='readonly_template_user', password='pwd')
        self.client.force_login(user)
        system_template = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='', name='System Base', content='fight_style=Patchwerk\n{player_config}',
        )

        update = self.client.put(
            f'/api/simc-template/?id={system_template.id}',
            data=json.dumps({'content': 'fight_style=HecticAddCleave\n{player_config}'}),
            content_type='application/json',
        )
        delete = self.client.delete(f'/api/simc-template/?id={system_template.id}')

        self.assertEqual(update.status_code, 403)
        self.assertEqual(delete.status_code, 405)
        system_template.refresh_from_db()
        self.assertEqual(system_template.content, 'fight_style=Patchwerk\n{player_config}')


class SimcAplCanonicalSpecPermissionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='apl-owner', password='pwd')
        self.admin = User.objects.create_user(username='lorexxar', password='pwd', is_staff=True)
        self.client.force_login(self.user)
        self.system_apl = SimcApl.objects.create(
            name='System Fury', spec='warrior_fury', class_name='warrior',
            content='actions+=/bloodthirst', source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True, is_active=True,
        )
        self.other_apl = SimcApl.objects.create(
            name='Other Fury', spec='warrior_fury', class_name='warrior',
            content='actions+=/whirlwind', source=SimcApl.SOURCE_USER,
            owner_user_id=self.admin.id, is_system=False, is_active=True,
        )

    def test_spec_options_are_canonical_and_include_midnight_devourer(self):
        response = self.client.get('/api/simc-spec-options/')
        self.assertEqual(response.status_code, 200)
        rows = response.json()['data']
        values = {row['value'] for row in rows}
        self.assertIn('warrior_fury', values)
        self.assertIn('demonhunter_devourer', values)
        self.assertNotIn('demon_hunter_devourer', values)
        devourer = next(row for row in rows if row['value'] == 'demonhunter_devourer')
        self.assertEqual(devourer['label'], '噬灭 · 恶魔猎手')

    def test_apl_update_rejects_unknown_spec(self):
        self.client.force_login(self.admin)
        response = self.client.put(
            f'/api/simc-workbench/apls/{self.system_apl.id}/',
            data=json.dumps({'spec': 'not_a_real_spec'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.system_apl.refresh_from_db()
        self.assertEqual(self.system_apl.spec, 'warrior_fury')

    def test_admin_can_update_and_delete_system_and_other_user_apl(self):
        self.client.force_login(self.admin)
        update = self.client.put(
            f'/api/simc-workbench/apls/{self.system_apl.id}/',
            data=json.dumps({'name': 'Updated Fury', 'spec': 'warrior_fury', 'content': 'actions+=/raging_blow'}),
            content_type='application/json',
        )
        self.assertEqual(update.status_code, 200)
        delete = self.client.delete(f'/api/simc-workbench/apls/{self.other_apl.id}/')
        self.assertEqual(delete.status_code, 200)
        self.assertFalse(SimcApl.objects.filter(id=self.other_apl.id).exists())


class SimcBackendUpdateSafetyTests(TestCase):
    def test_binary_update_removes_only_untracked_paths_now_tracked_upstream(self):
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command._set_status = __import__('unittest').mock.Mock()
        command.stdout = SimpleNamespace(write=lambda *args, **kwargs: None)
        command._run = __import__('unittest').mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout='', stderr='',
        ))
        responses = [
            SimpleNamespace(returncode=0, stdout='', stderr=''),
            SimpleNamespace(returncode=0, stdout=b'profiles/MID2/new.simc\0', stderr=b''),
            SimpleNamespace(
                returncode=0,
                stdout=b'profiles/MID2/new.simc\0build-cli/cache.txt\0',
                stderr=b'',
            ),
            SimpleNamespace(returncode=0, stdout='', stderr=''),
        ]
        with patch(
            'botend.management.commands.update_simc_binary.subprocess.run',
            side_effect=responses,
        ) as run:
            command._pull_rebase()

        self.assertEqual(command._run.call_args_list[0].args[0], [
            'git', 'clean', '-f', '--', 'profiles/MID2/new.simc',
        ])
        self.assertEqual(run.call_args_list[3].args[0], [
            'git', 'rebase', 'refs/remotes/origin/midnight',
        ])

    def test_upstream_check_fetches_explicit_midnight_branch(self):
        # Exercise the monitor helper because it owns the periodic
        # upstream check and must not resolve the checkout's implicit upstream.
        monitor = SimcMonitor(None, None)
        monitor.simc_source_dir = '/srv/simc'
        with patch.object(monitor, '_git_output', return_value='a' * 40) as git_output:
            self.assertEqual(monitor._get_git_upstream_hash(), 'a' * 40)
        self.assertEqual(git_output.call_args_list[0].args[0], [
            'fetch', '--prune', '--quiet', 'origin', 'midnight',
        ])
        self.assertEqual(git_output.call_args_list[1].args[0], [
            'rev-parse', 'refs/remotes/origin/midnight',
        ])

    def test_binary_update_fetches_and_rebases_explicit_midnight_branch(self):
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command._set_status = __import__('unittest').mock.Mock()
        command.stdout = SimpleNamespace(write=lambda *args, **kwargs: None)
        command._run = __import__('unittest').mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout='', stderr='',
        ))
        responses = [
            SimpleNamespace(returncode=0, stdout='', stderr=''),
            SimpleNamespace(returncode=0, stdout=b'', stderr=b''),
            SimpleNamespace(returncode=0, stdout=b'', stderr=b''),
            SimpleNamespace(returncode=0, stdout='', stderr=''),
        ]
        with patch(
            'botend.management.commands.update_simc_binary.subprocess.run',
            side_effect=responses,
        ) as run:
            command._pull_rebase()
        self.assertEqual(run.call_args_list[0].args[0], [
            'git', 'fetch', '--prune', 'origin', 'midnight',
        ])
        self.assertEqual(run.call_args_list[3].args[0], [
            'git', 'rebase', 'refs/remotes/origin/midnight',
        ])

    def test_binary_update_aborts_rebase_after_timeout(self):
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command._set_status = __import__('unittest').mock.Mock()
        command.stdout = SimpleNamespace(write=lambda *args, **kwargs: None)
        command._fail = __import__('unittest').mock.Mock(side_effect=CommandError('timeout'))
        responses = [
            SimpleNamespace(returncode=0, stdout='', stderr=''),
            SimpleNamespace(returncode=0, stdout=b'', stderr=b''),
            SimpleNamespace(returncode=0, stdout=b'', stderr=b''),
            __import__('subprocess').TimeoutExpired(['git', 'rebase'], 1800),
            SimpleNamespace(returncode=0, stdout='', stderr=''),
        ]
        with patch(
            'botend.management.commands.update_simc_binary.subprocess.run',
            side_effect=responses,
        ) as run:
            with self.assertRaises(CommandError):
                command._pull_rebase()

        self.assertEqual(run.call_args_list[-1].args[0], ['git', 'rebase', '--abort'])

    def test_tracked_source_changes_are_autocommitted_before_rebase_pull(self):
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command._run = __import__('unittest').mock.Mock()

        with patch('botend.management.commands.update_simc_binary.subprocess.run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=' M tracked.simc\n', stderr='')
            command._preserve_tracked_changes_before_pull()

        self.assertEqual(
            command._run.call_args_list,
            [
                __import__('unittest').mock.call(
                    ['git', 'add', '-u'], cwd='/srv/simc', timeout=30,
                    status='保存本地 SimC 源码改动', progress=8,
                ),
                __import__('unittest').mock.call(
                    ['git', 'commit', '-m', __import__('unittest').mock.ANY], cwd='/srv/simc', timeout=60,
                    status='提交本地 SimC 源码改动', progress=9,
                ),
            ],
        )
        commit_message = command._run.call_args_list[1].args[0][-1]
        self.assertIn('auto-save local changes before upstream sync', commit_message)

    def test_auto_update_failure_keeps_usable_binary_available_for_tasks(self):
        monitor = SimcMonitor(None, None)
        row = SimpleNamespace(simc_path=monitor.simc_path, auto_update=True, last_checked_at=None, is_updating=False)

        with patch.object(monitor, '_get_backend_row', return_value=row), \
             patch.object(monitor, '_validate_local_simc_binary', side_effect=[(True, ''), (True, '')]), \
             patch.object(monitor, '_get_git_hash', return_value='old123'), \
             patch.object(monitor, '_get_git_upstream_hash', return_value='new456'), \
             patch('django.core.management.call_command', side_effect=RuntimeError('compile failed')), \
             patch.object(monitor, '_set_update_status') as set_status, \
             patch('botend.controller.plugins.simc.SimcMonitor.upsert_system_alert'):
            self.assertTrue(monitor.ensure_local_simc_backend_current())

        self.assertTrue(any(
            kwargs.get('status') == '自动更新失败，继续使用现有 SimC 二进制'
            for _, kwargs in set_status.call_args_list
        ))


class SimcRawInspectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='simc_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_raw_inspection_endpoint_is_removed(self):
        response = self.client.post('/api/simc-profile/inspect-raw/', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 404)

    def test_raw_simc_task_create_persists_raw_code_in_ext(self):
        """Direct SimC code mode is no longer supported."""
        raw_code = 'mage="Arcaneone"\nspec=arcane\n'
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Arcaneone arcane 常规模拟',
                'simc_profile_id': 0,
                'raw_simc_code': raw_code,
                'regular_time': 300,
                'regular_target_count': 1,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('不再支持直接 SimC 代码模式', payload['error'])

    def test_raw_simc_attribute_task_is_rejected(self):
        """Direct SimC code mode is no longer supported."""
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'bad attribute raw',
                'simc_profile_id': 0,
                'raw_simc_code': 'paladin="Foo"\nspec=retribution\n',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('不再支持直接 SimC 代码模式', payload['error'])
        self.assertFalse(SimcTask.objects.exists())


@override_settings(SIMC_APL_CURRENT_IDENTITY=('test-revision', 'test-build'))
class SimcBatchVariableCompareTests(TestCase):
    def setUp(self):
        self.backend = get_test_backend()
        self.user = User.objects.create_user(username='comparison_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury',
            name='Batch contract base',
            content=(
                '{simulation_options}\n{player_config}\n'
                '{stat_overrides}\n{action_list}\n{output_options}\n'
            ),
            is_active=True,
        )
        self.default_apl = SimcApl.objects.create(
            name='Batch contract APL',
            spec='warrior_fury',
            content='actions=/auto_attack',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
            is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(b'actions=/auto_attack').hexdigest(),
            validation_revision=TEST_SIMC_REVISION, validation_game_build=TEST_WOW_BUILD,
        )
        self.apl_validation = patch(
            'botend.services.simc_task_service.validate_apl_for_profile',
            return_value={'valid': True,
                          'content_hash': hashlib.sha256(b'actions=/auto_attack').hexdigest(),
                          'revision': TEST_SIMC_REVISION, 'game_build': TEST_WOW_BUILD},
        )
        self.apl_validation.start()
        self.addCleanup(self.apl_validation.stop)
        self.default_player = SimcProfile.objects.create(
            user_id=None,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            spec='warrior_fury',
            class_name='warrior',
            name='MID1 Warrior Fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="FrozenArmory"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=1\nneck=,id=2\nshoulder=,id=3\nback=,id=4\nchest=,id=5\n'
                'wrist=,id=6\nhands=,id=7\nwaist=,id=8\nlegs=,id=9\nfeet=,id=10\n'
                'finger1=,id=11\nfinger2=,id=12\ntrinket1=,id=13\ntrinket2=,id=14\n'
                'main_hand=,id=15\noff_hand=,id=16'
            ),
            is_active=True,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Batch contract Profile',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            talent='BASE',
            is_active=True,
        )



    def test_parse_manual_candidates_canonicalizes_plural_slot_aliases(self):
        candidates = parse_manual_simc_candidates('''
warrior="Batcher"
level=90
spec=fury
shoulders=,id=212048
main_hand=,id=222222
### Gear from Bags
shoulders=,id=299001
''')
        self.assertEqual(candidates['gear_candidates'][0]['slot'], 'shoulder')



    def test_parse_manual_candidates_keeps_equipped_baseline_separate_from_bag_and_loadout_choices(self):
        candidates = parse_manual_simc_candidates('''
warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=212048,ilevel=639
### Gear from Bags
# Bag helm (650)
head=,id=299001,ilevel=650
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
### Weekly Reward Choices
# Weekly ring (655)
finger1=,id=299002,ilevel=655
''')
        self.assertEqual(candidates['base_talent'], 'ACTIVE_BUILD')
        self.assertEqual(candidates['gear_candidates'][0]['slot'], 'head')
        self.assertEqual(candidates['gear_candidates'][0]['item_id'], 299001)
        self.assertEqual(candidates['gear_candidates'][0]['source'], 'bags')
        self.assertEqual(candidates['gear_candidates'][1]['source'], 'weekly_reward')
        self.assertEqual(candidates['talent_candidates'][0]['talent'], 'CLEAVE_BUILD')
        self.assertEqual(parse_manual_player_config('head=,id=212048\n### Gear from Bags\nhead=,id=299001', 'fury')['equipment'][0]['id'], 212048)

    def test_profile_detail_parses_consumables_and_talent_strings(self):
        detail = build_player_config_detail(
            'manual_equipment', 'deathknight_frost',
            player_equipment=(
                'deathknight="Tester"\n'
                'spec=frost\n'
                'talents=BUILDCODE\n'
                'class_talents=s207104:1/s444040:2\n'
                'spec_talents=s194912:1\n'
                'hero_talents=s555555:1\n'
                'potion=potion_of_testing\n'
                'flask=flask_of_testing\n'
                'food=food_of_testing\n'
                'augmentation=void_touched\n'
                'temporary_enchant=main_hand:oil_a/off_hand:oil_b\n'
                'head=,id=212048'
            ),
            talent='MODEL_BUILD',
        )
        self.assertEqual(detail['consumables']['potion'], 'potion_of_testing')
        self.assertEqual(detail['consumables']['temporary_enchant']['main_hand'], 'oil_a')
        self.assertEqual(detail['talent_strings']['talents']['value'], 'MODEL_BUILD')
        self.assertEqual(detail['talent_strings']['class_talents']['value'], 's207104:1/s444040:2')
        self.assertEqual(detail['talent_strings']['class_talents']['entries'][1], {'spell_id': 444040, 'rank': 2})
        self.assertEqual(detail['talent_strings']['hero_talents']['entries'][0]['spell_id'], 555555)

    def test_parse_manual_candidates_splits_real_addon_profile_from_commented_extras(self):
        parsed = parse_simc_player_profile('''
# SimC Addon 12.0.7-01
warrior="炎色雷灬"
level=90
spec=fury
talents=ACTIVE_BUILD
head=,id=249952,enchant_id=8017
neck=,id=249337
main_hand=,id=251078
# Saved Loadout: 团本山丘
# talents=RAID_HILL_BUILD
### Gear from Bags
#
# 盘绕恶意丝带 (285)
# neck=,id=299001,bonus_id=6652/13668
#
# 流光织锦披风 (289)
# back=,id=299002,bonus_id=13440/41
### Weekly Reward Choices
#
# 每周宝库头盔 (298)
# head=,id=299003,bonus_id=13786
### End of Weekly Reward Choices
### Additional Character Info
# upgrade_currencies=c:3347:267
''')

        self.assertEqual(parsed['profile']['identity']['class_name'], 'warrior')
        self.assertEqual(parsed['profile']['identity']['name'], '炎色雷灬')
        self.assertEqual(parsed['profile']['identity']['spec'], 'fury')
        self.assertEqual(parsed['profile']['talents']['build_code'], 'ACTIVE_BUILD')
        self.assertEqual(
            [row['slot'] for row in parsed['profile']['equipment']],
            ['head', 'neck', 'main_hand'],
        )
        self.assertNotIn('Gear from Bags', parsed['profile']['raw_player_block'])
        self.assertNotIn('Saved Loadout', parsed['profile']['raw_player_block'])
        self.assertEqual(
            [(row['slot'], row['item_id'], row['source']) for row in parsed['candidates']['gear']],
            [('neck', 299001, 'bags'), ('back', 299002, 'bags'), ('head', 299003, 'weekly_reward')],
        )
        self.assertEqual(
            parsed['candidates']['talents'],
            [{'name': '团本山丘', 'talent': 'RAID_HILL_BUILD', 'source': 'saved_loadout'}],
        )

    def test_parse_simc_player_profile_splits_current_block_from_commented_candidates(self):
        parsed = parse_simc_player_profile('''
warrior="KBZ"
level=90
spec=fury
talents=BASE_BUILD
head=,id=212048
main_hand=,id=222222
# Saved Loadout: 团本山丘
# talents=RAID_BUILD
### Gear from Bags
# 盘绕恶意丝带 (285)
# neck=,id=249337,bonus_id=6652/13668
### Weekly Reward Choices
# Reward Ring (289)
# finger1=,id=251115
''')
        self.assertEqual(parsed['profile']['identity']['name'], 'KBZ')
        self.assertEqual(parsed['profile']['talents']['build_code'], 'BASE_BUILD')
        self.assertEqual(parsed['profile']['raw_player_block'].count('head='), 1)
        self.assertEqual(parsed['candidates']['talents'], [
            {'name': '团本山丘', 'talent': 'RAID_BUILD', 'source': 'saved_loadout'},
        ])
        self.assertEqual(
            [(row['slot'], row['item_id'], row['source']) for row in parsed['candidates']['gear']],
            [('neck', 249337, 'bags'), ('finger1', 251115, 'weekly_reward')],
        )
        self.assertEqual(parsed['candidates']['gear'][0]['name'], '盘绕恶意丝带')
        self.assertEqual(parsed['candidates']['gear'][0]['item_level'], 285)
        self.assertEqual(parsed['profile']['talents']['saved_loadouts'], [])

    def test_attribute_continuation_endpoint_rejects_client_managed_rounds(self):
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'continue_task_id': 123,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()['success'])
        self.assertIn('Worker', response.json()['error'])

    @patch('botend.dashboard.api.create_task_from_request')
    def test_product_admin_can_compare_with_global_ptr_wcl_profile(self, create_task):
        admin = User.objects.create_user(
            username='comparison_admin', password='pwd', is_staff=True,
        )
        self.client.force_login(admin)
        profile = SimcProfile.objects.create(
            user_id=None,
            source=SimcProfile.SOURCE_WCL,
            name='12.1 PTR WCL Fury',
            spec='warrior_fury',
            class_name='warrior',
            use_ptr=True,
            player_config_mode='wcl',
            player_equipment=(
                'warrior="PTR"\nlevel=90\nspec=fury\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            talent='PTR_BASE',
            is_active=True,
        )
        create_task.return_value = SimpleNamespace(id=9981, mode='comparison')

        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'talent_candidates',
            'name': 'PTR WCL talent comparison',
            'spec': 'warrior_fury',
            'simc_profile_id': profile.id,
            'player_source': {'type': 'saved_profile', 'profile_id': profile.id},
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'fight_style': 'Patchwerk',
            'time': 60,
            'target_count': 1,
            'candidates': [{
                'name': 'PTR candidate',
                'talent': 'PTR_CANDIDATE',
                'source': 'manual',
            }],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertEqual(response.json()['data']['task_id'], 9981)
        create_task.assert_called_once()

    def test_auto_attribute_batch_creates_complete_50_rating_pairwise_neighborhood(self):
        base = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        rows = SimcComparisonTaskAPIView._attribute_variants(base, 50)
        self.assertEqual(len(rows), 13)
        self.assertEqual(sum(is_base for _, _, is_base, _ in rows), 1)
        moves = [candidate['move'] for _, _, is_base, candidate in rows if not is_base]
        self.assertEqual(
            {(move['from'], move['to'], move['transfer']) for move in moves},
            {(source, target, 50) for source in base for target in base if source != target},
        )
        for _, ratings, _, candidate in rows:
            self.assertEqual(sum(ratings.values()), sum(base.values()))
            self.assertTrue(all(value >= 0 for value in ratings.values()))
            if candidate['move'].get('type') != 'baseline':
                self.assertEqual(candidate['move']['transfer'], 50)

    def test_auto_attribute_batch_omits_sub_50_source_without_projecting_non_grid_move(self):
        base = {'crit': 49, 'haste': 50, 'mastery': 100, 'versatility': 0}
        rows = SimcComparisonTaskAPIView._attribute_variants(base, 50)
        moves = [candidate['move'] for _, _, is_base, candidate in rows if not is_base]
        self.assertEqual(len(rows), 7)  # centre + (haste/mastery) * 3 valid targets
        self.assertTrue(all(move['from'] != 'crit' for move in moves))
        self.assertTrue(all(move['transfer'] == 50 for move in moves))


    def test_auto_attribute_batch_accepts_simc_addon_source_and_freezes_attribute_profile(self):
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury 即时属性寻优',
            'spec': 'warrior_fury',
            'player_source': {
                'type': 'simc_addon',
                'simc_code': (
                    'warrior="Imported"\nlevel=90\nspec=fury\ntalents=IMPORT_BUILD\n'
                    'gear_crit=1000\ngear_haste=2000\ngear_mastery=3000\ngear_versatility=4000\n'
                    'head=,id=212048\nmain_hand=,id=222222\nactions=/auto_attack'
                ),
            },
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 100, 'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertEqual(response.json()['data']['mode'], 'attribute_sweep')
        self.assertNotIn('task_type', response.json()['data'])
        self.assertNotIn('result_file', response.json()['data'])
        task = SimcTask.objects.get(id=response.json()['data']['task_id'])
        profile = task.profile_version.payload
        self.assertEqual(profile['player_config_mode'], 'attribute_only')
        self.assertEqual(profile['spec'], 'fury')
        self.assertEqual(profile['talent'], '')
        self.assertEqual(
            [profile['gear_crit'], profile['gear_haste'], profile['gear_mastery'], profile['gear_versatility']],
            [None, None, None, None],
        )
        self.assertNotIn('actions=', profile['player_equipment'])
        self.assertEqual(task.simulation_runs.count(), 0)
        self.assertEqual(len(task.mode_params['initial_candidates']), 13)
        self.assertEqual(
            task.mode_params['initial_candidates'][0]['candidate_params']['attribute_ratings'],
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000},
        )

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_auto_attribute_batch_accepts_battlenet_source_with_frozen_ratings(self, preflight):
        preflight.return_value = {
            'simc_ready': True, 'warnings': [],
            'simc_config': {
                'player_config_mode': 'battlenet', 'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak', 'battlenet_character': 'Batcher',
                'spec': 'fury', 'talent': '', 'gear_strength': 10000,
                'gear_crit': 1000, 'gear_haste': 2000,
                'gear_mastery': 3000, 'gear_versatility': 4000,
            },
        }
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury Battle.net 属性寻优',
            'spec': 'warrior_fury',
            'player_source': {'type': 'battlenet', 'region': 'eu', 'realm': 'Kazzak', 'character': 'Batcher'},
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 100,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['task_id'])
        profile = task.profile_version.payload
        self.assertEqual(profile['player_config_mode'], 'attribute_only')
        self.assertIn('warrior="FrozenArmory"', profile['player_equipment'])
        self.assertEqual([profile['gear_crit'], profile['gear_haste'], profile['gear_mastery'], profile['gear_versatility']], [None, None, None, None])
        self.assertEqual(task.simulation_runs.count(), 0)
        self.assertEqual(len(task.mode_params['initial_candidates']), 13)
        self.assertEqual(
            task.mode_params['initial_candidates'][0]['candidate_params']['attribute_ratings'],
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000},
        )
        from botend.services.simc_task_service import initialize_task_runs
        runs = initialize_task_runs(task)
        monitor = SimcMonitor(None, None)
        rendered = []
        for run in runs[:2]:
            request_data = monitor.apply_candidate_overrides({
                'spec': task.profile_version.payload['spec'],
                'fight_style': task.simulation_params.get('fight_style', 'Patchwerk'),
                'time': task.simulation_params.get('max_time', 300),
                'target_count': task.simulation_params.get('desired_targets', 1),
                'player_import_mode': task.profile_version.payload['player_config_mode'],
                'player_equipment': task.profile_version.payload['player_equipment'],
                'talent': task.profile_version.payload['talent'],
                'gear_strength': task.profile_version.payload['gear_strength'],
                'gear_crit': task.profile_version.payload['gear_crit'],
                'gear_haste': task.profile_version.payload['gear_haste'],
                'gear_mastery': task.profile_version.payload['gear_mastery'],
                'gear_versatility': task.profile_version.payload['gear_versatility'],
                'base_template_content': task.template_version.payload['content'],
                'override_action_list': task.apl_version.payload['content'],
                '_result_file_path': f'/tmp/simc_run_{run.id}.html',
            }, run.candidate_params)
            simc_code, _, error = SimcComposer(self.user.id).compose(request_data)
            self.assertFalse(error)
            rendered.append(simc_code)
        self.assertNotEqual(rendered[0], rendered[1])
        self.assertIn('gear_crit_rating=1000', rendered[0])
        self.assertIn('gear_crit_rating=900', rendered[1])
        self.assertNotIn('armory=', rendered[0])

    def test_auto_attribute_batch_rejects_missing_frozen_player_baseline(self):
        self.profile.player_config_mode = 'attribute_only'
        self.profile.player_equipment = ''
        self.profile.save(update_fields=['player_config_mode', 'player_equipment'])
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury 自动属性比较',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 100, 'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('玩家装备基线', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())




    def test_auto_attribute_batch_keeps_fixed_grid_moves(self):
        # 50-rating 离散搜索不允许把不足一步的余额投影成 100 等非网格转移。
        base = {'crit': 400, 'haste': 1100, 'mastery': 1140, 'versatility': 100}
        rows = SimcComparisonTaskAPIView._attribute_variants(base, 50)
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(sum(ratings.values()) == sum(base.values()) for _, ratings, _, _ in rows))
        self.assertTrue(all(candidate['move'].get('type') == 'baseline' or candidate['move']['transfer'] == 50 for _, _, _, candidate in rows))


    def test_next_attribute_round_preserves_budget_and_marks_new_center(self):
        base = {'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800}
        rows = SimcComparisonTaskAPIView._attribute_variants(base, 50, round_number=2, mark_base=True)
        self.assertEqual(len(rows), 13)
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[0][3]['round'], 2)
        self.assertTrue(all(sum(ratings.values()) == 10000 for _, ratings, _, _ in rows))




    def test_attribute_detail_parses_frozen_player_and_overlays_requested_ratings(self):
        detail = build_player_config_detail(
            'attribute_only', 'fury',
            player_equipment='warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\nhead=,id=212048,ilevel=639',
            talent='ATTRIBUTE_BUILD', gear_strength=5000,
            gear_crit=1000, gear_haste=2000, gear_mastery=3000, gear_versatility=4000,
        )

        self.assertEqual(detail['identity']['name'], 'Batcher')
        self.assertEqual(detail['identity']['level'], 90)
        self.assertEqual(detail['equipment'][0]['id'], 212048)
        self.assertEqual(detail['talents']['build_code'], 'ATTRIBUTE_BUILD')
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 1000)
        self.assertNotIn('无装备', detail['source']['label'])

    def test_devourer_detail_uses_demon_hunter_rule_and_mastery_coefficient(self):
        from botend.models import SimcMasteryCoefficient, SimcSecondaryStatRule

        SimcSecondaryStatRule.objects.update_or_create(
            class_name='demon_hunter',
            defaults={
                'crit_per_percent': 46, 'haste_per_percent': 44,
                'mastery_per_percent': 46, 'versatility_per_percent': 54,
            },
        )
        SimcMasteryCoefficient.objects.update_or_create(
            spec='demonhunter_devourer', defaults={'mastery_coefficient': 1.0},
        )

        detail = build_player_config_detail(
            'attribute_only', 'devourer',
            player_equipment=(
                'demonhunter="Devourer"\nlevel=90\nspec=devourer\n'
                'talents=BASE\nhead=,id=212048'
            ),
            talent='BUILD', gear_mastery=460,
        )

        self.assertEqual(detail['identity']['class_name'], 'demonhunter')
        self.assertEqual(detail['stats']['secondary']['mastery']['percent'], 10.0)

    def test_battlenet_template_selection_accepts_playerless_default_template(self):
        monitor = SimcMonitor(None, None)
        default_template = SimpleNamespace(
            id=1,
            spec='default',
            content='fight_style={fight_style}\n{player_config}\n{action_list}',
        )
        selected = monitor._select_template_from_queryset(
            [default_template], 'blood', player_config_mode='battlenet'
        )
        self.assertIs(selected, default_template)

    def test_template_selection_ignores_non_executable_probe_template(self):
        monitor = SimcMonitor(None, None)
        probe = SimpleNamespace(id=1, spec='default', content='spec={spec}\n{player_config}\n')
        executable = SimpleNamespace(
            id=2,
            spec='default',
            content='warrior="Template"\nspec={spec}\n',
        )
        selected = monitor._select_template_from_queryset([probe, executable], 'fury')
        self.assertIs(selected, executable)

    def test_incomplete_base_template_is_not_executable(self):
        probe = SimpleNamespace(id=1, content='spec=fury\n{player_config}\n')
        self.assertFalse(SimcMonitor._is_executable_base_template(probe))

    def test_simc_error_details_keep_attribute_batch_execution_context(self):
        monitor = SimcMonitor(None, None)
        manifest = {
            'player_config_mode': 'attribute_only',
            'spec': 'fury',
            'talent': 'ATTRIBUTE_BUILD',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
            'selected_apl_id': 42,
            'comparison_context': {'task_id': 1, 'candidate': {'round': 1}},
        }
        task = SimpleNamespace(ext=json.dumps(manifest), id=99)

        monitor.save_simc_error_details(task, 'SimC未生成预期结果文件', stderr_text='x' * 20000)

        stored = json.loads(task.ext)
        for key, value in manifest.items():
            self.assertEqual(stored[key], value)
        self.assertEqual(stored['simc_error_summary'], 'SimC未生成预期结果文件')
        self.assertIn('simc_error_native', stored)

    def test_attribute_batch_task_renders_its_own_explicit_html_result_file(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="LMonitor"\n{player_config}\nhtml={result_file}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'BUILD',
                'gear_strength': 5000,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'result_file': 'simc_task_42.html',
            },
        )
        self.assertIn('html=simc_task_42.html', rendered)
        self.assertIn('warrior="Frozen"', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertNotIn('warrior="LMonitor"', rendered)
        self.assertNotRegex(rendered, r'(?m)^\s*gear_strength\s*=')
        self.assertIn('gear_crit_rating=1000', rendered)
        self.assertIn('gear_haste_rating=2000', rendered)
        self.assertIn('gear_mastery_rating=3000', rendered)
        self.assertIn('gear_versatility_rating=4000', rendered)
        self.assertNotIn('\ncrit_rating=1000', rendered)
        self.assertNotIn('{result_file}', rendered)

    def test_attribute_batch_task_appends_explicit_html_when_base_template_has_no_placeholder(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="LMonitor"\n{player_config}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'BUILD',
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'result_file': 'simc_task_43.html',
            },
        )
        self.assertTrue(rendered.endswith('html=simc_task_43.html'))
        self.assertEqual(rendered.count('html='), 1)

    def test_attribute_render_replaces_singular_talent_with_selected_build(self):
        rendered = SimcMonitor(None, None).apply_template(
            '{player_config}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalent=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'NEW',
            },
        )
        self.assertIn('talents=NEW', rendered)
        self.assertNotIn('talent=OLD', rendered)

    def test_attribute_render_drops_executable_bag_and_weekly_alternatives(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="Template"\n{player_config}\n{action_list}',
            {
                'player_config_mode': 'attribute_only', 'talent': 'BUILD',
                'player_equipment': (
                    'warrior="Frozen"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222\n'
                    '### Gear from Bags\nhead=,id=299001\n'
                    '### Weekly Reward Choices\nfinger1=,id=299002'
                ),
            },
        )
        self.assertIn('head=,id=212048', rendered)
        self.assertNotIn('299001', rendered)
        self.assertNotIn('299002', rendered)
        self.assertNotIn('Gear from Bags', rendered)

    def test_attribute_task_rejects_nonempty_baseline_without_actor_or_equipped_slot(self):
        for baseline in ('head=,id=212048', 'warrior="No gear"\nspec=fury'):
            response = self.client.post('/api/simc-task/', data=json.dumps({
                'name': 'Malformed baseline', 'spec': 'fury',
                'player_config_mode': 'attribute_only', 'player_equipment': baseline,
                'talent': 'BUILD', 'gear_crit': 1, 'gear_haste': 2,
                'gear_mastery': 3, 'gear_versatility': 4,
            }), content_type='application/json')
            self.assertFalse(response.json()['success'], response.json())
        self.assertFalse(SimcTask.objects.exists())

    def test_result_file_directive_replaces_existing_html_output(self):
        rendered = SimcMonitor.ensure_result_file_directive(
            'warrior="LMonitor"\nhtml=stale_report.html\n',
            'simc_task_44.html',
        )
        self.assertEqual(rendered.count('html='), 1)
        self.assertTrue(rendered.endswith('html=simc_task_44.html'))
        self.assertNotIn('stale_report.html', rendered)


    def test_execute_simc_command_passes_absolute_task_result_path(self):
        from unittest.mock import patch
        import tempfile
        import os
        monitor = object.__new__(SimcMonitor)
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.simc_path = '/opt/simc'
            monitor.result_path = tmpdir
            task = SimpleNamespace(
                id=88, result_file='simc_task_88.html', ext='{}',
                result_summary='', error_detail='', save=lambda **kwargs: None,
                backend=self.backend, backend_id=self.backend.id,
            )
            expected = os.path.join(tmpdir, task.result_file)
            with patch(
                    'botend.controller.plugins.simc.SimcMonitor.os.path.isfile',
                    side_effect=lambda path: path == self.backend.simc_path or Path(path).is_file()), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.access', return_value=True), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.sched_getaffinity', return_value={0, 1}), \
                 patch('botend.controller.plugins.simc.SimcMonitor.subprocess.Popen') as popen:
                process = popen.return_value
                def communicate(**kwargs):
                    Path(expected).write_text('<html></html>', encoding='utf-8')
                    return (
                        'Player: Audit warrior fury 90\n  DPS=60000.0\n    bloodthirst Count=40 pDPS=5000\n',
                        '',
                    )
                process.communicate.side_effect = communicate
                process.returncode = 0
                with patch('botend.interface.ossupload.ossUpload', return_value=True):
                    self.assertTrue(monitor.execute_simc_command('/tmp/input.simc', task, task.result_file))
            self.assertEqual(
                popen.call_args.args[0],
                ['/opt/simc', '/tmp/input.simc', f'html={expected}', 'threads=1'],
            )
            self.assertEqual(popen.call_args.kwargs['env']['LANG'], 'C')
            self.assertEqual(popen.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_execute_simc_command_terminates_process_when_claim_is_cancelled(self):
        from unittest.mock import patch
        import subprocess
        import tempfile

        monitor = object.__new__(SimcMonitor)
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.result_path = tmpdir
            task = SimpleNamespace(
                id=288, pk=288, result_file='simc_task_288.html', ext='{}',
                result_summary='', error_detail='', save=lambda **kwargs: None,
                backend=self.backend, backend_id=self.backend.id,
            )
            with patch('botend.controller.plugins.simc.SimcMonitor.os.path.isfile',
                       side_effect=lambda path: path == self.backend.simc_path), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.access', return_value=True), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.sched_getaffinity', return_value={0, 1}), \
                 patch('botend.controller.plugins.simc.SimcMonitor.subprocess.Popen') as popen, \
                 patch.object(monitor, '_active_task_claim_is_current', return_value=False):
                process = popen.return_value
                process.communicate.side_effect = [
                    subprocess.TimeoutExpired('/opt/simc', 1), ('', ''),
                ]
                process.poll.return_value = None
                self.assertFalse(monitor.execute_simc_command('/tmp/input.simc', task, task.result_file))
            process.terminate.assert_called_once_with()
            process.kill.assert_not_called()
            self.assertEqual(process.communicate.call_count, 2)

    def test_execute_simc_command_kills_process_that_ignores_terminate(self):
        from unittest.mock import patch
        import subprocess
        import tempfile

        monitor = object.__new__(SimcMonitor)
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.result_path = tmpdir
            task = SimpleNamespace(
                id=289, pk=289, result_file='simc_task_289.html', ext='{}',
                result_summary='', error_detail='', save=lambda **kwargs: None,
                backend=self.backend, backend_id=self.backend.id,
            )
            with patch('botend.controller.plugins.simc.SimcMonitor.os.path.isfile',
                       side_effect=lambda path: path == self.backend.simc_path), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.access', return_value=True), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.sched_getaffinity', return_value={0, 1}), \
                 patch('botend.controller.plugins.simc.SimcMonitor.subprocess.Popen') as popen, \
                 patch.object(monitor, '_active_task_claim_is_current', return_value=False):
                process = popen.return_value
                process.communicate.side_effect = [
                    subprocess.TimeoutExpired('/opt/simc', 1),
                    subprocess.TimeoutExpired('/opt/simc', 5),
                    ('', ''),
                ]
                process.poll.return_value = None
                self.assertFalse(monitor.execute_simc_command('/tmp/input.simc', task, task.result_file))
            process.terminate.assert_called_once_with()
            process.kill.assert_called_once_with()
            self.assertEqual(process.communicate.call_count, 3)

    def test_execute_simc_command_rejects_ptr_binary_live_fallback_warning(self):
        from unittest.mock import patch
        import tempfile
        import os
        monitor = object.__new__(SimcMonitor)
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.result_path = tmpdir
            task = SimpleNamespace(
                id=188, result_file='simc_task_188.html', ext='{}',
                result_summary='', error_detail='', save=lambda **kwargs: None,
                backend=self.backend, backend_id=self.backend.id,
            )
            expected = os.path.join(tmpdir, task.result_file)
            with patch(
                    'botend.controller.plugins.simc.SimcMonitor.os.path.isfile',
                    side_effect=lambda path: path == self.backend.simc_path or Path(path).is_file()), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.access', return_value=True), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.sched_getaffinity', return_value={0, 1}), \
                 patch('botend.controller.plugins.simc.SimcMonitor.subprocess.Popen') as popen:
                process = popen.return_value
                def communicate(**kwargs):
                    Path(expected).write_text('<html></html>', encoding='utf-8')
                    return (
                        'SimulationCraft 1200-01',
                        "SimulationCraft has not been built with PTR data. The 'ptr=' option is ignored.",
                    )
                process.communicate.side_effect = communicate
                process.returncode = 0
                self.assertFalse(monitor.execute_simc_command('/tmp/ptr.simc', task, task.result_file))
            self.assertIn('不支持 PTR 数据', task.error_detail)

    def test_execute_simc_command_caps_requested_threads_to_leave_one_cpu_for_web(self):
        task = SimpleNamespace(simulation_params={'threads': 4})
        with patch('os.sched_getaffinity', return_value={0, 1}):
            self.assertEqual(SimcMonitor.runtime_threads(task), 1)

    def test_runtime_threads_uses_available_capacity_when_task_has_no_override(self):
        task = SimpleNamespace(simulation_params={})
        with patch('os.sched_getaffinity', return_value=set(range(8))):
            self.assertEqual(SimcMonitor.runtime_threads(task), 7)

    def test_execute_simc_command_rejects_auto_attack_only_semantic_result(self):
        from unittest.mock import patch
        import tempfile
        import os
        monitor = object.__new__(SimcMonitor)
        stdout = '''Player: Audit warrior fury 90
  DPS=2422.9 DPS-Error=9.1/0.38%
    auto_attack_mh Count=112.6 pDPS=1618
    auto_attack_oh Count=110.4 pDPS=803
    charge_impact Count=1.0 pDPS=2
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.simc_path = '/opt/simc'
            monitor.result_path = tmpdir
            task = SimpleNamespace(
                id=89, result_file='simc_task_89.html',
                ext=json.dumps({'spec': 'fury'}),
                result_summary='', error_detail='',
                save=lambda **kwargs: None,
                backend=self.backend, backend_id=self.backend.id,
            )
            expected = os.path.join(tmpdir, task.result_file)
            with patch(
                    'botend.controller.plugins.simc.SimcMonitor.os.path.isfile',
                    side_effect=lambda path: path == self.backend.simc_path or Path(path).is_file()), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.access', return_value=True), \
                 patch('botend.controller.plugins.simc.SimcMonitor.os.sched_getaffinity', return_value={0, 1}), \
                 patch('botend.controller.plugins.simc.SimcMonitor.subprocess.Popen') as popen:
                process = popen.return_value
                def communicate(**kwargs):
                    with open(expected, 'w', encoding='utf-8') as report:
                        report.write('<html></html>')
                    return stdout, ''
                process.communicate.side_effect = communicate
                process.returncode = 0
                self.assertFalse(monitor.execute_simc_command('/tmp/input.simc', task, task.result_file))
        stored = json.loads(task.ext)
        self.assertIn('只有自动攻击', stored['simc_error_summary'])
        self.assertEqual(stored['semantic_validation']['valid'], False)

    def test_semantic_validation_identifies_unresolved_talent_apl_dispatch(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=2499.2 DPS-Error=20/0.82%
  Priorities (actions.default):
    auto_attack/charge,if=time<=0.5
    run_action_list,name=slayer,if=talent.slayers_dominance&active_enemies=1
    run_action_list,name=thane,if=talent.lightning_strikes&active_enemies=1
  Actions:
    auto_attack_mh Count=48.7 pDPS=1672
    auto_attack_oh Count=47.5 pDPS=823
    charge_impact Count=1.0 pDPS=5
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertFalse(validation['valid'])
        self.assertEqual(validation['failure_type'], 'talent_apl_dispatch')
        self.assertEqual(validation['unresolved_action_lists'], ['slayer', 'thane'])
        self.assertIn('英雄天赋', validation['reason'])
        self.assertIn('slayer', validation['reason'])
        self.assertIn('thane', validation['reason'])

    def test_semantic_validation_rejects_unresolved_talent_dispatch_with_item_proc_damage(self):
        stdout = '''Player: Audit warrior arms 90
  DPS=3209.8 DPS-Error=2.5/0.08%
  Priorities (actions.default):
    auto_attack
    run_action_list,name=colossus_st,if=talent.demolish&active_enemies=1
    run_action_list,name=slayer_st,if=talent.slayers_dominance&active_enemies=1
  Actions:
    auto_attack_mh Count=125.0 pDPS=2920
    voidclaw Count=35.9 pDPS=288
    charge_impact Count=1.0 pDPS=2
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertFalse(validation['valid'])
        self.assertEqual(validation['failure_type'], 'talent_apl_dispatch')
        self.assertEqual(validation['unresolved_action_lists'], ['colossus_st', 'slayer_st'])

    def test_semantic_validation_identifies_single_unresolved_talent_dispatch(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=2499.2 DPS-Error=20/0.82%
  Priorities (actions.default):
    auto_attack
    run_action_list,name=hero,if=talent.hero_root
  Actions:
    auto_attack_mh Count=48.7 pDPS=1672
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertEqual(validation['failure_type'], 'talent_apl_dispatch')
        self.assertEqual(validation['unresolved_action_lists'], ['hero'])

    def test_semantic_validation_rejects_report_invalid_weapon_actions(self):
        stdout = '''Player: MID2_Rogue_Outlaw
DPS=27230.56 DPS-Error=43.77/0.16%
  auto_attack_mh Count=50 pDPS= 9000
  sinister_strike Count=40 pDPS= 18000
'''
        report_html = '''<html><body>
<div class="section section-open"><h2>Trivial</h2><ul>
<li>Player 'MID2_Rogue_Outlaw' attempting to use Action 'dispatch' (2098) with invalid main-hand weapon type 'Dagger'.</li>
<li>Player 'MID2_Rogue_Outlaw' attempting to use Action 'blade_rush' (271877) with invalid main-hand weapon type 'Dagger'.</li>
</ul></div></body></html>'''
        validation = SimcMonitor.validate_simulation_semantics(stdout, report_html=report_html)
        self.assertFalse(validation['valid'])
        self.assertEqual(validation['failure_type'], 'invalid_weapon_action')
        self.assertEqual(len(validation['report_errors']), 2)
        self.assertIn('dispatch', validation['reason'])

    def test_semantic_validation_keeps_nonfatal_report_warnings_valid(self):
        stdout = '''Player: Frost
DPS=208365 DPS-Error=200/0.1%
  frostbolt Count=42 pDPS= 208365
'''
        report_html = "<h2>Trivial</h2><li>The 'icicles' expression is deprecated.</li>"
        validation = SimcMonitor.validate_simulation_semantics(stdout, report_html=report_html)
        self.assertTrue(validation['valid'])
        self.assertEqual(validation['report_errors'], [])

    def test_semantic_validation_does_not_misclassify_when_a_talent_dispatch_is_active(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=2499.2 DPS-Error=20/0.82%
  Priorities (actions.default):
    auto_attack
    run_action_list,name=slayer,if=talent.slayers_dominance
    run_action_list,name=thane,if=talent.lightning_strikes
  Priorities (actions.slayer):
    bloodthirst,if=0
  Actions:
    auto_attack_mh Count=48.7 pDPS=1672
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertEqual(validation['failure_type'], 'auto_attack_only')
        self.assertEqual(validation['unresolved_action_lists'], ['thane'])

    def test_semantic_validation_accepts_active_talent_dispatch_in_slash_joined_priorities(self):
        stdout = '''Player: Audit warrior arms 90
  DPS=331722.2 DPS-Error=622.2/0.19%
  Priorities (actions.default):
    auto_attack/run_action_list,name=colossus_aoe,if=talent.demolish&active_enemies>2/run_action_list,name=colossus_st,if=talent.demolish/run_action_list,name=slayer_aoe,if=talent.slayers_dominance&active_enemies>2/run_action_list,name=slayer_st,if=talent.slayers_dominance
  Priorities (actions.slayer_aoe):
    bladestorm,if=0
  Actions:
    auto_attack_mh Count=125.0 pDPS=4762
    whirlwind Count=35.9 pDPS=326960
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertTrue(validation['valid'])
        self.assertEqual(validation['failure_type'], '')
        self.assertEqual(
            validation['unresolved_action_lists'],
            ['colossus_aoe', 'colossus_st', 'slayer_st'],
        )

    def test_semantic_validation_accepts_non_talent_sibling_dispatch_when_talent_branch_is_inactive(self):
        stdout = '''Player: Audit mage frost 90
  DPS=208368.3 DPS-Error=121.1/0.06%
  Priorities (actions.default):
    call_action_list,name=cds/run_action_list,name=ff_tarswap,if=talent.frostfire_bolt&variable.target_swapping/run_action_list,name=ff_aoe,if=talent.frostfire_bolt&active_enemies>=3/run_action_list,name=ff_st,if=talent.frostfire_bolt/run_action_list,name=ss_aoe,if=active_enemies>=4/run_action_list,name=ss_st
  Priorities (actions.ss_aoe):
    frozen_orb
  Actions:
    frostbolt Count=42.0 pDPS=208365
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertTrue(validation['valid'])
        self.assertEqual(
            validation['unresolved_action_lists'],
            ['ff_tarswap', 'ff_aoe', 'ff_st'],
        )

    def test_semantic_validation_accepts_terminal_unconditional_sibling_dispatch(self):
        stdout = '''Player: Audit deathknight frost 90
  DPS=177000.0 DPS-Error=125.0/0.07%
  Priorities (actions.default):
    run_action_list,name=aoe,if=active_enemies>=3/run_action_list,name=single_target
  Priorities (actions.single_target):
    obliterate
  Actions:
    obliterate Count=42.0 pDPS=161158
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertTrue(validation['valid'])
        self.assertEqual(validation['failure_type'], '')

    def test_semantic_validation_accepts_hero_tree_sibling_dispatch_when_talent_branches_are_inactive(self):
        stdout = '''Player: Audit warrior protection 90
  DPS=197565.4 DPS-Error=182.4/0.09%
  Priorities (actions.default):
    auto_attack/run_action_list,name=colossus_aoe,if=hero_tree.colossus&active_enemies>=3/run_action_list,name=thane_aoe,if=hero_tree.mountain_thane&active_enemies>=3/run_action_list,name=colossus_st,if=talent.demolish/run_action_list,name=thane_st,if=talent.lightning_strikes
  Priorities (actions.thane_aoe):
    thunder_blast
  Actions:
    thunder_blast Count=26.8 pDPS=263805
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertTrue(validation['valid'])
        self.assertEqual(
            validation['unresolved_action_lists'],
            ['colossus_st', 'thane_st'],
        )

    def test_semantic_validation_accepts_core_skill_damage(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=62453.0 DPS-Error=150/0.24%
  Priorities (actions.slayer):
    auto_attack_mh Count=144.6 pDPS=3390
    bloodthirst Count=43.3 pDPS=4976
    rampage1 Count=79.6 pDPS=2295
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertTrue(validation['valid'])
        self.assertGreater(validation['non_auto_dps'], 0)
        self.assertEqual(validation['dps_error'], 150.0)
        self.assertEqual(validation['dps_error_pct'], 0.24)

    def test_attribute_search_requires_100_as_initial_step(self):
        self.profile.player_config_mode = 'attribute_only'
        self.profile.save(update_fields=['player_config_mode'])
        bad_response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'attribute_variants', 'name': '错误步长',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 50,
        }), content_type='application/json')
        self.assertFalse(bad_response.json()['success'])
        self.assertIn('从 100 绿字步长开始', bad_response.json()['error'])




    def test_batch_rejects_unsupported_source_and_oversized_candidate_selection(self):
        base = {
            'name': 'Manual candidate compare',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
        }
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({**base, 'kind': 'gear_candidates', 'candidates': [{'slot': 'head', 'item_id': 1, 'source': 'external'}]}), content_type='application/json')
        self.assertFalse(response.json()['success'])
        self.assertIn('来源', response.json()['error'])
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({**base, 'kind': 'gear_candidates', 'candidates': [{'slot': 'head', 'item_id': 200000 + i, 'source': 'bags'} for i in range(8)]}), content_type='application/json')
        self.assertFalse(response.json()['success'])
        self.assertIn('最多', response.json()['error'])


    def test_legacy_two_stat_scan_honors_50_rating_steps_and_keeps_baseline(self):
        monitor = SimcMonitor(None, None)
        points = monitor.build_attribute_test_points(total_value=4000, base_value=1700, requested_step=50)
        self.assertEqual(points[0], 0)
        self.assertEqual(points[-1], 4000)
        self.assertIn(1700, points)
        self.assertEqual(points, list(range(0, 4001, 50)))
        self.assertEqual(points, sorted(set(points)))

    def _comparison_task_with_runs(self, mode='comparison', rows=()):
        task = create_test_task(
            user_id=self.user.id, name='comparison report', simc_profile_id=0,
            mode=mode, current_status=0,
        )
        for index, row in enumerate(rows):
            label, status, dps, params = row
            SimulationRun.objects.create(
                task=task, sequence=index + 1, candidate_key=f'candidate-{index}',
                candidate_label=label, status=status, candidate_params=params,
                result_summary={} if dps is None else {'dps': dps},
            )
        return task

    def test_selected_ordinary_tasks_can_generate_a_cross_report_comparison(self):
        first = create_test_task(user_id=self.user.id, name='结果 A', simc_profile_id=0, mode='regular', current_status=2)
        second = create_test_task(user_id=self.user.id, name='结果 B', simc_profile_id=0, mode='regular', current_status=2)
        SimulationRun.objects.create(task=first, sequence=1, candidate_key='result-a', status='completed', result_summary={'dps': 1000})
        SimulationRun.objects.create(task=second, sequence=1, candidate_key='result-b', status='completed', result_summary={'dps': 1100})
        payload = self.client.get('/api/simc-regular-compare/', {'task_ids': f'{first.id},{second.id}'}).json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual([row['name'] for row in payload['data']['runs']], ['结果 A', '结果 B'])
        self.assertEqual(payload['data']['comparison']['winner']['id'], second.id)

    def test_selected_tasks_expose_and_render_actual_frozen_input_differences(self):
        profile_versions = [
            SimcResourceVersion.objects.create(
                resource_type='profile', resource_id=self.profile.id,
                content_hash=f'compare-profile-{suffix}', payload={
                    'name': name, 'spec': 'warrior_fury',
                    'player_config_mode': 'manual_equipment',
                    'player_equipment': (
                        f'warrior="Batcher"\nspec=fury\ntalents={talent}\n'
                        f'head={item_name},id={item_id},ilevel={item_level}'
                    ),
                },
            )
            for suffix, name, talent, item_name, item_id, item_level in (
                ('base', '基准 Profile', 'BASE_TALENT', '基准头盔', 111, 650),
                ('candidate', '候选 Profile', 'ALT_TALENT', '候选头盔', 222, 660),
            )
        ]
        apl_versions = [
            SimcResourceVersion.objects.create(
                resource_type='apl', resource_id=self.default_apl.id,
                content_hash=f'compare-apl-{suffix}',
                payload={'name': name, 'spec': 'warrior_fury', 'content': content},
            )
            for suffix, name, content in (
                ('base', '单体 APL', 'actions=/bloodthirst'),
                ('candidate', '多目标 APL', 'actions=/whirlwind'),
            )
        ]
        template_version = SimcResourceVersion.objects.create(
            resource_type='template', resource_id=self.base_template.id,
            content_hash='compare-template-base',
            payload={'name': '统一模板', 'spec': 'warrior_fury', 'content': self.base_template.content},
        )
        tasks = []
        for index, (profile_version, apl_version, targets, dps) in enumerate(zip(
                profile_versions, apl_versions, (1, 5), (1000, 1100)), start=1):
            task = create_test_task(
                user_id=self.user.id, name=f'冻结输入 {index}', simc_profile_id=self.profile.id,
                mode='normal', current_status=2, profile=self.profile,
                template=self.base_template, apl=self.default_apl,
                profile_version=profile_version, template_version=template_version,
                apl_version=apl_version,
                simulation_params={
                    'fight_style': 'Patchwerk', 'desired_targets': targets,
                    'max_time': 300, 'iterations': 10000,
                },
            )
            run = SimulationRun.objects.create(
                task=task, sequence=1, candidate_key=f'result-{index}',
                status='completed', result_summary={'dps': dps},
            )
            SimcTaskArtifact.objects.create(
                task=task, run=run, artifact_type='html_report',
                file_path=f'simc_agent_results/compare-{index}.html',
            )
            self.assertEqual(
                SimcComparisonTaskAPIView._run_result_file(run),
                f'simc_agent_results/compare-{index}.html',
            )
            tasks.append(task)

        report_html = (
            '<div class="player"><div class="toggle-content">'
            '<script type="text/x-deferred-html">'
            '<table class="sc sort"><thead><tr>'
            '<th>Damage Stats</th><th>DPS</th><th>DPS%</th>'
            '</tr></thead><tbody>'
            '<tr class="toprow"><td>{ability}</td>'
            '<td>({ability_dps})</td><td>({percent}%)</td></tr>'
            '</tbody></table>'
            '</script></div></div>'
        )
        with patch.object(
            SimcRegularCompareAPIView,
            '_get_result_file_content',
            side_effect=(
                report_html.format(ability='Bloodthirst', ability_dps=600, percent=60),
                report_html.format(ability='Whirlwind', ability_dps=770, percent=70),
            ),
        ):
            payload = self.client.get(
                '/api/simc-regular-compare/',
                {'task_ids': ','.join(str(task.id) for task in tasks)},
            ).json()

        self.assertTrue(payload['success'], payload)
        baseline, candidate = payload['data']['runs']
        self.assertEqual(baseline['input_difference_summary'], '对比基准')
        self.assertEqual(baseline['input_differences'], [])
        differences = {row['key']: row for row in candidate['input_differences']}
        self.assertEqual(differences['profile']['before'], '基准 Profile')
        self.assertEqual(differences['profile']['after'], '候选 Profile')
        self.assertEqual(differences['apl']['before'], '单体 APL')
        self.assertEqual(differences['apl']['after'], '多目标 APL')
        self.assertEqual(differences['simulation.desired_targets']['before'], '1')
        self.assertEqual(differences['simulation.desired_targets']['after'], '5')
        self.assertEqual(differences['talent']['before'], 'BASE_TALENT')
        self.assertEqual(differences['talent']['after'], 'ALT_TALENT')
        self.assertIn('ID 111', differences['equipment.head']['before'])
        self.assertIn('ID 222', differences['equipment.head']['after'])
        self.assertEqual(baseline['abilities'], [
            {'name': 'Bloodthirst', 'dps': '600', 'dps_percent': '60%'},
        ])
        self.assertEqual(candidate['abilities'], [
            {'name': 'Whirlwind', 'dps': '770', 'dps_percent': '70%'},
        ])
        self.assertEqual(baseline['apl_difference_summary'], '对比基准')
        self.assertEqual(baseline['apl_differences'], [])
        self.assertEqual(candidate['apl_difference_summary'], '1 行新增，1 行删除')
        self.assertEqual(candidate['apl_differences'], [
            {'type': 'removed', 'line': 'actions=/bloodthirst'},
            {'type': 'added', 'line': 'actions=/whirlwind'},
        ])
        compare_template = (Path(__file__).resolve().parents[2] / 'templates/simc_regular_compare.html').read_text()
        self.assertIn('实际输入差异（相对基准）', compare_template)
        self.assertIn('APL内容对比（相对基准）', compare_template)
        self.assertIn('apl_differences', compare_template)
        self.assertIn('技能DPS占比对比', compare_template)
        self.assertIn('data-apl-language="cn"', compare_template)
        self.assertIn("fetch('/api/convert-text/'", compare_template)
        self.assertIn("conversion_type: 'apl_to_cn'", compare_template)
        self.assertIn('spec: aplModalState.spec', compare_template)
        self.assertIn('input_differences', compare_template)

    def test_selected_comparison_can_read_other_users_task_results(self):
        other_user = User.objects.create_user(username='comparison_other', password='pwd')
        own = create_test_task(user_id=self.user.id, name='我的结果', simc_profile_id=0, mode='regular', current_status=2)
        other = create_test_task(user_id=other_user.id, name='他人结果', simc_profile_id=0, mode='regular', current_status=2)
        SimulationRun.objects.create(
            task=own, sequence=1, candidate_key='own-result', status='completed',
            result_summary={'dps': 1000},
        )
        SimulationRun.objects.create(
            task=other, sequence=1, candidate_key='other-result', status='completed',
            result_summary={'dps': 1100},
        )
        response = self.client.get('/api/simc-regular-compare/', {'task_ids': f'{own.id},{other.id}'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertEqual(
            [row['id'] for row in response.json()['data']['runs']],
            [own.id, other.id],
        )

    def test_legacy_comparison_detail_apis_are_not_owner_scoped(self):
        other_user = User.objects.create_user(username='comparison_detail_other', password='pwd')
        task = self._comparison_task_with_runs('comparison', [
            ('基准', 'completed', 1000, {'is_base': True}),
            ('候选', 'completed', 1100, {'is_base': False}),
        ])
        task.user_id = other_user.id
        task.save(update_fields=['user_id'])

        for url in (
            f'/api/simc-task/comparison/?task_id={task.id}',
            f'/api/simc-regular-compare/?task_id={task.id}',
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'], response.json())

    def test_selected_comparison_requires_two_task_ids(self):
        task = create_test_task(user_id=self.user.id, name='单个结果', simc_profile_id=0, mode='regular', current_status=2)
        response = self.client.get('/api/simc-regular-compare/', {'task_ids': str(task.id)})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_attribute_task_report_returns_real_dps_rankings_path_and_refinement_state(self):
        base = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        rows = []
        for label, ratings, is_base, candidate in SimcComparisonTaskAPIView._attribute_variants(base, 50):
            rows.append((label, 'completed', 100000 if is_base else 99900, {
                'candidate_type': 'attribute_ratings', 'is_base': is_base,
                'attribute_ratings': ratings, 'search': candidate,
            }))
        task = self._comparison_task_with_runs('attribute_sweep', rows)
        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()
        self.assertTrue(payload['success'], payload)
        report = payload['data']['attribute_report']
        self.assertEqual(report['algorithm'], 'four_stat_pairwise_hill_climb')
        self.assertEqual(report['step'], 50)
        self.assertEqual(report['total_rating'], 10000)
        self.assertEqual(report['rounds_completed'], 1)
        self.assertEqual(report['recommendation']['ratings'], base)
        self.assertEqual(report['stop_reason'], 'refining_step')
        self.assertFalse(report['converged'])
        self.assertEqual(len(report['candidates']), 13)
        self.assertTrue(all('result_file' not in row for row in report['candidates']))

    def test_completed_attribute_report_uses_persisted_error_aware_conclusion(self):
        base = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        rows = []
        for index, (label, ratings, is_base, candidate) in enumerate(
                SimcComparisonTaskAPIView._attribute_variants(base, 50)):
            dps = 100000
            if index == 1:
                dps = 100384
            rows.append((label, 'completed', dps, {
                'candidate_type': 'attribute_ratings', 'is_base': is_base,
                'attribute_ratings': ratings, 'search': candidate,
            }))
        task = self._comparison_task_with_runs('attribute_sweep', rows)
        task.current_status = 2
        task.analysis_result = {'attribute_search': {
            'ratings': base, 'dps': 100000, 'step': 50, 'round': 1,
            'converged': True, 'stop_reason': 'local_optimum_50_pairwise',
        }}
        task.save(update_fields=['current_status', 'analysis_result'])
        for run in task.simulation_runs.all():
            run.result_summary['dps_error'] = 268
            run.save(update_fields=['result_summary'])

        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()

        self.assertTrue(payload['success'], payload)
        report = payload['data']['attribute_report']
        self.assertTrue(report['local_optimum'])
        self.assertEqual(report['stop_reason'], 'local_optimum_50_pairwise')
        self.assertEqual(report['recommendation']['ratings'], base)
        self.assertEqual(report['recommendation']['dps'], 100000)

    def test_running_attribute_task_keeps_best_result_provisional_and_lists_current_round_queue(self):
        center = {'crit': 1100, 'haste': 900, 'mastery': 800, 'versatility': 0}
        candidate = {'crit': 1080, 'haste': 920, 'mastery': 800, 'versatility': 0}
        task = self._comparison_task_with_runs('attribute_sweep', [
            ('基准属性', 'completed', 100000, {
                'candidate_type': 'attribute_ratings', 'is_base': True,
                'attribute_ratings': center,
                'search': {'type': 'attribute', 'round': 4, 'step': 20, 'candidate_index': 0},
            }),
            ('暴击 -20 / 急速 +20', 'running', None, {
                'candidate_type': 'attribute_ratings', 'is_base': False,
                'attribute_ratings': candidate,
                'search': {'type': 'attribute', 'round': 4, 'step': 20, 'candidate_index': 1},
            }),
            ('急速 -20 / 暴击 +20', 'pending', None, {
                'candidate_type': 'attribute_ratings', 'is_base': False,
                'attribute_ratings': center,
                'search': {'type': 'attribute', 'round': 4, 'step': 20, 'candidate_index': 2},
            }),
        ])
        task.current_status = 1
        task.save(update_fields=['current_status'])
        task.simulation_runs.update(round_number=4)

        payload = self.client.get(f'/api/simc-workbench/tasks/{task.id}/').json()

        self.assertTrue(payload['success'], payload)
        report = payload['data']['attribute_report']
        self.assertFalse(report['local_optimum'])
        self.assertNotIn('final_result', report)
        current_round_runs = [
            run for run in payload['data']['runs'] if run['round_number'] == report['current_round']
        ]
        self.assertEqual(
            [run['status'] for run in current_round_runs],
            ['completed', 'running', 'pending'],
        )
        detail = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-detail.js').read_text()
        attribute_renderer = detail[
            detail.index('function renderAttributeTask'):detail.index('function renderTaskComparison')
        ]
        self.assertIn("const searchConverged = attribute.converged === true || Number(row.status) === 2", attribute_renderer)
        self.assertIn('当前最佳结果', attribute_renderer)
        self.assertIn('currentRoundRuns', attribute_renderer)
        self.assertIn('当前轮候选', attribute_renderer)

    def test_attribute_task_detail_is_dedicated_and_links_the_persisted_final_run_report(self):
        initial = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        final = {'crit': 1050, 'haste': 1950, 'mastery': 3000, 'versatility': 4000}
        task = self._comparison_task_with_runs('attribute_sweep', [
            ('初始属性', 'completed', 100000, {
                'candidate_type': 'attribute_ratings', 'is_base': True,
                'attribute_ratings': initial, 'search': {'round': 1, 'candidate_index': 0},
            }),
            ('暴击 +50 / 急速 -50', 'completed', 101000, {
                'candidate_type': 'attribute_ratings', 'is_base': False,
                'attribute_ratings': final, 'search': {'round': 1, 'candidate_index': 1},
            }),
        ])
        initial_run, final_run = task.simulation_runs.order_by('sequence')
        SimcTaskArtifact.objects.create(
            task=task, run=initial_run, artifact_type='html_report',
            file_path='simc_results/attribute-initial.html',
        )
        final_artifact = SimcTaskArtifact.objects.create(
            task=task, run=final_run, artifact_type='html_report',
            file_path='simc_results/attribute-final.html',
        )
        task.current_status = 2
        task.analysis_result = {'attribute_search': {
            'ratings': final, 'dps': 101000, 'step': 50, 'round': 1,
            'converged': True, 'stop_reason': 'local_optimum_50_pairwise',
        }}
        task.save(update_fields=['current_status', 'analysis_result'])

        payload = self.client.get(f'/api/simc-workbench/tasks/{task.id}/').json()

        self.assertTrue(payload['success'], payload)
        report = payload['data']['attribute_report']
        self.assertEqual(report['search_path'][0]['ratings'], initial)
        self.assertEqual(report['final_result']['id'], final_run.id)
        self.assertEqual(report['final_result']['ratings'], final)
        self.assertEqual(
            report['final_result']['report_url'],
            f'/api/simc-workbench/artifacts/{final_artifact.id}/preview/',
        )
        detail = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-detail.js').read_text()
        attribute_renderer = detail[
            detail.index('function renderAttributeTask'):detail.index('function renderTaskComparison')
        ]
        self.assertIn("if (row.mode === 'attribute_sweep')", detail)
        self.assertIn('attribute.search_path', attribute_renderer)
        self.assertIn('attribute.final_result', attribute_renderer)
        self.assertIn('查看最终结果报告', attribute_renderer)
        for generic_section in ('SimcResultReport', '技能伤害与触发明细', '动态 Buff / Proc', 'Artifact / 原生报告'):
            self.assertNotIn(generic_section, attribute_renderer)

    def test_attribute_final_report_prioritizes_final_run_stats_and_marginal_gains(self):
        initial = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        final = {'crit': 1200, 'haste': 1800, 'mastery': 3100, 'versatility': 3900}
        task = self._comparison_task_with_runs('attribute_sweep', [
            ('初始属性', 'completed', 100000, {
                'candidate_type': 'attribute_ratings', 'is_base': True,
                'attribute_ratings': initial, 'search': {'round': 1, 'candidate_index': 0},
            }),
            ('最终推荐', 'completed', 102000, {
                'candidate_type': 'attribute_ratings', 'is_base': False,
                'attribute_ratings': final, 'search': {'round': 1, 'candidate_index': 1},
            }),
            ('边际暴击 +100', 'completed', 102100, {
                'candidate_type': 'attribute_ratings', 'is_base': False,
                'attribute_ratings': {**final, 'crit': 1300},
                'search': {'type': 'attribute_marginal_gain', 'round': 2, 'candidate_index': 0},
            }),
        ])
        _, final_run, marginal_run = task.simulation_runs.order_by('sequence')
        final_artifact = SimcTaskArtifact.objects.create(
            task=task, run=final_run, artifact_type='html_report',
            file_path='simc_results/attribute-final-percentages.html',
        )
        SimcTaskArtifact.objects.create(
            task=task, run=marginal_run, artifact_type='html_report',
            file_path='simc_results/attribute-marginal-percentages.html',
        )
        task.current_status = 2
        task.analysis_result = {'attribute_search': {
            'ratings': final, 'dps': 102000, 'step': 20, 'round': 1,
            'converged': True, 'stop_reason': 'local_optimum_20_pairwise',
            'marginal_gain_status': 'completed',
            'marginal_gains': [{
                'run_id': marginal_run.id, 'stat': 'crit', 'amount': 100,
                'ratings': {**final, 'crit': 1300}, 'baseline_dps': 102000,
                'dps': 102100, 'dps_gain': 100, 'gain_percent': 0.098,
            }],
        }}
        task.save(update_fields=['current_status', 'analysis_result'])
        final_report = {'sections': [{'key': 'stats', 'tables': [{'rows': [
            [{'text': ''}, {'text': 'Raid-Buffed'}, {'text': 'Unbuffed'}],
            [{'text': 'Crit'}, {'text': '21.50% (1200)'}, {'text': '18.50% (1200)'}],
            [{'text': 'Haste'}, {'text': '18.25% (1800)'}, {'text': '15.25% (1800)'}],
            [{'text': 'Mastery'}, {'text': '44.00% (3100)'}, {'text': '40.00% (3100)'}],
            [{'text': 'Versatility'}, {'text': '12.75% (3900)'}, {'text': '9.75% (3900)'}],
        ]}]}]}
        marginal_report = {'sections': [{'key': 'stats', 'tables': [{'rows': [
            [{'text': ''}, {'text': 'Raid-Buffed'}, {'text': 'Unbuffed'}],
            [{'text': 'Crit'}, {'text': '99.99% (1300)'}, {'text': '88.88% (1300)'}],
        ]}]}]}

        with patch(
            'botend.services.simc_result_analysis.analyze_run_artifact',
            side_effect=lambda _task, artifact: (
                final_report if artifact.id == final_artifact.id else marginal_report
            ),
        ):
            payload = self.client.get(f'/api/simc-workbench/tasks/{task.id}/').json()

        self.assertTrue(payload['success'], payload)
        final_result = payload['data']['attribute_report']['final_result']
        self.assertEqual(final_result['id'], final_run.id)
        self.assertEqual(final_result['unbuffed_stats'], {
            'crit': '18.50%', 'haste': '15.25%',
            'mastery': '40.00%', 'versatility': '9.75%',
        })
        detail = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-detail.js').read_text()
        renderer = detail[detail.index('function renderAttributeTask'):detail.index('function renderTaskComparison')]
        self.assertIn('finalResult?.unbuffed_stats', renderer)
        self.assertIn('团队增益前', renderer)
        self.assertIn('attribute-stat-percent', renderer)
        self.assertLess(renderer.index('attribute-final-result'), renderer.index("card('收敛后边际收益'"))
        self.assertLess(renderer.index("card('收敛后边际收益'"), renderer.index("card('当前轮候选'"))

    def test_regular_candidate_task_returns_only_safe_summary(self):
        task = self._comparison_task_with_runs(rows=[
            ('基准配置', 'completed', 1744, {'is_base': True, 'search': {'candidate_index': 0}}),
            ('候选天赋', 'completed', 1801, {'is_base': False, 'search': {'candidate_index': 1}}),
        ])
        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()
        self.assertTrue(payload['success'], payload)
        rows = payload['data']['runs']
        self.assertEqual([row['dps'] for row in rows], [1744, 1801])
        self.assertTrue(all('candidate_params' not in row for row in rows))

    def test_task_run_relation_is_authoritative_and_read_has_no_lifecycle_side_effect(self):
        task = self._comparison_task_with_runs(rows=[
            ('基准配置', 'completed', 100000, {'is_base': True, 'search': {'candidate_index': 0}}),
            ('候选配置', 'completed', 101000, {'is_base': False, 'search': {'candidate_index': 1}}),
        ])
        unrelated = self._comparison_task_with_runs(rows=[
            ('不应混入', 'completed', 999999, {'is_base': False, 'search': {'candidate_index': 0}}),
        ])
        before = (task.current_status, task.completed_at)
        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual([row['label'] for row in payload['data']['runs']], ['基准配置', '候选配置'])
        self.assertNotEqual(task.id, unrelated.id)
        task.refresh_from_db()
        self.assertEqual((task.current_status, task.completed_at), before)

    def test_comparison_query_reports_pending_progress(self):
        task = self._comparison_task_with_runs(rows=[
            ('baseline', 'pending', None, {'is_base': True}),
            ('candidate', 'running', None, {'is_base': False}),
        ])
        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['task']['pending'], 1)
        self.assertEqual(payload['data']['task']['running'], 1)
        self.assertEqual(payload['data']['task']['succeeded'], 0)

    def test_scan_processes_comparison_task_once_not_each_run(self):
        task = self._comparison_task_with_runs(rows=[
            ('first', 'pending', None, {}), ('second', 'pending', None, {}),
        ])
        monitor = SimcMonitor(None, None)
        with patch.object(monitor, 'ensure_local_simc_backend_current', return_value=True), \
             patch('botend.controller.plugins.simc.SimcMonitor.os.path.exists', return_value=True), \
             patch('botend.controller.plugins.simc.SimcMonitor.os.path.isfile', return_value=True), \
             patch.object(monitor, 'process_simc_task', return_value=True) as process:
            self.assertTrue(monitor.scan())
        self.assertEqual([call.args[0].id for call in process.call_args_list], [task.id])

    def test_reference_task_drains_all_pending_runs_in_one_dispatch(self):
        task = self._comparison_task_with_runs(rows=[
            ('first', 'pending', None, {}), ('second', 'pending', None, {}), ('third', 'pending', None, {}),
        ])
        monitor = SimcMonitor(None, None)
        processed = []
        def finish(_task, run):
            processed.append(run.id)
            run.status = 'completed'
            run.result_summary = {'dps': 100 + run.sequence}
            run.save(update_fields=['status', 'result_summary'])
        with patch.object(monitor, 'process_reference_run', side_effect=finish):
            self.assertTrue(monitor.process_reference_task(task))
        self.assertEqual(processed, list(task.simulation_runs.order_by('sequence').values_list('id', flat=True)))
        task.refresh_from_db()
        self.assertEqual(task.current_status, 2)

    def test_compare_response_ranks_candidates_against_explicit_baseline(self):
        task = self._comparison_task_with_runs(rows=[
            ('基准配置', 'completed', 100000, {'is_base': True, 'search': {'candidate_index': 0}}),
            ('候选 A', 'completed', 103000, {'is_base': False, 'search': {'candidate_index': 1}}),
            ('候选 B', 'completed', 99000, {'is_base': False, 'search': {'candidate_index': 2}}),
        ])
        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()
        rows = payload['data']['runs']
        self.assertEqual([(row['label'], row['rank']) for row in rows], [('基准配置', 2), ('候选 A', 1), ('候选 B', 3)])
        self.assertEqual([row['delta_dps'] for row in rows], [0, 3000, -1000])
        self.assertEqual([row['delta_percent'] for row in rows], [0.0, 3.0, -1.0])
        self.assertEqual(payload['data']['comparison']['baseline']['label'], '基准配置')
        self.assertEqual(payload['data']['comparison']['winner']['label'], '候选 A')


class SimcNewConfigModeTests(TestCase):
    """测试新版工作台任务配置：只输入玩家信息，战斗/APL 由选项控制。"""

    def setUp(self):
        self.user = User.objects.create_user(username='newmode_user', password='pwd')
        group = DashboardUserGroup.objects.create(
            name='SimC history test group', permission_codes=['simc.history'], is_active=True,
        )
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}\n{simulation_options}\n{stat_overrides}\n{output_options}',
            is_active=True,
        )
        self.default_apl = SimcApl.objects.create(
            name='Default APL',
            spec='warrior_fury',
            content='actions=/auto_attack\nactions+=/bloodthirst',
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
            is_active=True,
        )

    def test_attribute_manifest_task_is_rejected_until_reference_architecture(self):
        """Legacy attribute tasks without 6-reference fields are rejected."""
        task = create_test_task(
            user_id=self.user.id,
            name='Manifest attribute snapshot',
            task_type=2,
            simc_profile_id=0,
            ext=json.dumps({
                'player_config_mode': 'attribute_only',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 50,
                'gear_strength': 0,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
            }),
            current_status=0,
            is_active=True,
        )
        monitor = SimcMonitor(None, None)
        result = monitor.process_simc_task(task)
        self.assertFalse(result)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)

    def test_direct_attribute_task_persists_full_manifest_snapshot(self):
        """Task type 2 (old attribute sweep) is now rejected."""
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Direct attribute snapshot',
                'task_type': 2,
                'player_import_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=SNAPSHOT_BUILD\nhead=,id=212048\nmain_hand=,id=222222',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 50,
                'gear_strength': 0,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'fight_style': 'DungeonSlice',
                'time': 180,
                'target_count': 5,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('task_type', payload['error'])

    def test_task_list_does_not_expose_raw_simc_code(self):
        task = create_test_task(
            user_id=self.user.id,
            name='private raw code',
            task_type=1,
            simc_profile_id=0,
            ext=json.dumps({'raw_simc_code': 'warrior="secret"\nspec=fury\n', 'spec': 'fury'}),
        )

        response = self.client.get('/api/simc-task/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        listed = next(row for row in payload['data'] if row['id'] == task.id)
        self.assertNotIn('ext', listed)
        self.assertNotIn('raw_simc_code', listed['ext_detail'])
        self.assertNotIn('secret', json.dumps(payload, ensure_ascii=False))

    def test_task_create_response_does_not_expose_raw_simc_code(self):
        """raw_simc_code mode is now rejected; verify no secret leaks in error."""
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'new private raw code',
                'simc_profile_id': 0,
                'raw_simc_code': 'warrior="create-secret"\nspec=fury\n',
                'regular_time': 300,
                'regular_target_count': 1,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('不再支持直接 SimC 代码模式', payload['error'])
        self.assertNotIn('create-secret', json.dumps(payload, ensure_ascii=False))

    @patch('botend.dashboard.api.create_task')
    def test_normal_task_can_use_read_only_upstream_system_profile(self, create_task_mock):
        from types import SimpleNamespace
        from django.utils import timezone

        system_profile = SimcProfile.objects.create(
            user_id=None,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            class_name='warrior',
            name='MID1 Fury player',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Default"\nlevel=90\nspec=fury\nmain_hand=,id=222222',
        )
        create_task_mock.return_value = SimpleNamespace(
            id=901,
            name='System profile smoke',
            simc_profile_id=system_profile.id,
            current_status=0,
            mode='normal',
            create_time=timezone.now(),
            modified_time=timezone.now(),
        )

        detail_response = self.client.get(
            f'/api/simc-player-config-detail/?profile_id={system_profile.id}'
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertTrue(detail_response.json()['success'], detail_response.json())

        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'System profile smoke',
                'spec': 'fury',
                'simc_profile_id': system_profile.id,
                'player_source': {'type': 'saved_profile', 'profile_id': system_profile.id},
                'base_template_id': self.base_template.id,
                'selected_apl_id': self.default_apl.id,
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'backend_id': 7,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertEqual(
            create_task_mock.call_args.kwargs['profile_id'],
            system_profile.id,
        )


    def test_task_ext_summary_drops_raw_simc_code_from_browser_response(self):
        summary = SimcTaskAPIView()._task_ext_summary(1, json.dumps({
            'raw_simc_code': 'warrior="secret"\nspec=fury\n',
            'metadata': {'raw_simc_code': 'nested-secret'},
            'player_equipment': 'warrior="equipment-secret"',
            'override_action_list': 'actions=secret_action',
            'spec': 'fury',
            'time': 300,
        }))

        self.assertNotIn('raw_simc_code', summary)
        self.assertNotIn('metadata', summary)
        self.assertNotIn('player_equipment', summary)
        self.assertNotIn('override_action_list', summary)
        self.assertNotIn('simc_error_native', summary)
        self.assertNotIn('secret', json.dumps(summary, ensure_ascii=False))
        self.assertEqual(summary['spec'], 'fury')
        self.assertEqual(summary['time'], 300)

    def test_task_ext_summary_keeps_safe_apl_context_without_apl_source(self):
        summary = SimcTaskAPIView()._task_ext_summary(1, json.dumps({
            'selected_apl_id': 42,
            'apl_compare': {
                'task_id': 42,
                'candidate_index': 2,
                'candidate_name': '候选方案2',
                'candidate_reason': '交换技能优先级',
                'is_base': False,
                'preprocess_stage': 'ready',
                'preprocess_error': '无',
                'preprocess_reasoning': '包含不应进入浏览器的推理全文',
                'apl_list': 'actions=/secret_action',
            },
        }))

        self.assertEqual(summary['selected_apl_id'], 42)
        self.assertEqual(summary['apl_compare'], {
            'task_id': 42,
            'candidate_index': 2,
            'is_base': False,
            'preprocess_stage': 'ready',
        })
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn('candidate_name', serialized)
        self.assertNotIn('candidate_reason', serialized)
        self.assertNotIn('preprocess_error', serialized)
        self.assertNotIn('preprocess_reasoning', serialized)
        self.assertNotIn('secret_action', serialized)

    def test_task_list_hides_failed_native_result_output(self):
        task = create_test_task(
            user_id=self.user.id,
            name='failed raw task',
            task_type=1,
            simc_profile_id=0,
            current_status=3,
            result_file='SimC执行失败\\n错误输出: warrior="result-secret"',
        )

        response = self.client.get('/api/simc-task/')
        self.assertEqual(response.status_code, 200)
        listed = next(row for row in response.json()['data'] if row['id'] == task.id)
        self.assertNotIn('result_file', listed)
        self.assertNotIn('result-secret', json.dumps(response.json(), ensure_ascii=False))

    def test_attribute_analysis_ssr_is_not_owner_scoped(self):
        other_user = User.objects.create_user(username='attribute_ssr_other', password='pwd')
        task = create_test_task(
            user_id=other_user.id,
            name='SSR attribute report',
            task_type=2,
            simc_profile_id=0,
            current_status=2,
            result_file='77_gear_crit_850_gear_haste_979.html',
        )
        result_file = f'{task.id}_gear_crit_850_gear_haste_979.html'
        task.result_file = result_file
        task.save(update_fields=['result_file'])
        response_mock = SimpleNamespace(status_code=200, text='Bloodmastêr: 123,456 dps')

        with patch('botend.dashboard.dashboard.settings.OSS_CONFIG', {'base_url': 'https://oss.example/'}, create=True), \
             patch('requests.get', return_value=response_mock):
            response = self.client.get(f'/simc-attribute-analysis-ssr/?task_id={task.id}')

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertContains(response, '123456')
        self.assertContains(response, 'gear_crit')

    def test_attribute_analysis_api_reads_foreign_task_but_only_its_registered_reports(self):
        other_user = User.objects.create_user(username='attribute_api_other', password='pwd')
        task = create_test_task(
            user_id=other_user.id,
            name='API attribute report',
            task_type=2,
            simc_profile_id=0,
            current_status=2,
        )
        owned_file = f'{task.id}_gear_crit_850_gear_haste_979.html'
        foreign_file = f'{task.id + 1}_gear_crit_900_gear_haste_929.html'
        task.result_file = f'{owned_file},{foreign_file},legacy_crit_1_haste_2.html'
        task.save(update_fields=['result_file'])
        response_mock = SimpleNamespace(status_code=200, text='Bloodmastêr: 123,456 dps')

        with patch('botend.dashboard.api.settings.OSS_CONFIG', {'base_url': 'https://oss.example/'}, create=True), \
             patch('requests.get', return_value=response_mock):
            response = self.client.get(f'/api/simc-attribute-analysis/?task_id={task.id}')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(len(payload['data']['results']), 1)
        self.assertEqual(payload['data']['results'][0]['file_name'], owned_file)
        self.assertEqual(payload['data']['results'][0]['attr1_name'], 'gear_crit')
        self.assertEqual(payload['data']['results'][0]['attr1_value'], 850)
        self.assertEqual(payload['data']['results'][0]['dps'], 123456)

    def test_preview_manifest_is_not_owner_scoped(self):
        task = create_test_task(
            user_id=self.user.id,
            name='Preview manifest task',
            task_type=1,
            simc_profile_id=0,
            current_status=2,
            result_file='preview-task.html',
            ext=json.dumps({
                'player_config_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'spec': 'blood',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'gear_strength': 0,
                'gear_crit': 850,
                'gear_haste': 979,
                'gear_mastery': 641,
                'gear_versatility': 69,
            }),
        )

        response = self.client.get(f'/api/simc-task/preview/?task_id={task.id}')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['id'], task.id)
        self.assertEqual(payload['data']['spec'], 'blood')
        self.assertEqual(payload['data']['gear']['strength'], 0)
        self.assertEqual(payload['data']['gear']['haste'], 979)
        self.assertNotIn('raw_simc_code', payload['data'])

        other = User.objects.create_user(username='preview_other_user', password='pwd')
        self.client.force_login(other)
        foreign_detail = self.client.get(f'/api/simc-task/preview/?task_id={task.id}')
        self.assertEqual(foreign_detail.status_code, 200)
        self.assertTrue(foreign_detail.json()['success'], foreign_detail.json())
        self.assertEqual(foreign_detail.json()['data']['id'], task.id)

    def test_result_proxy_is_not_owner_scoped_for_legacy_task_results(self):
        other = User.objects.create_user(username='result_proxy_other', password='pwd')
        task = create_test_task(
            user_id=other.id,
            name='Foreign legacy result',
            simc_profile_id=0,
            mode='regular',
            current_status=2,
            result_file='foreign-legacy-result.html',
        )
        response_mock = SimpleNamespace(status_code=200, text='<html>foreign result</html>')

        with patch('botend.dashboard.api.settings.OSS_CONFIG', {'base_url': 'https://oss.example/'}, create=True), \
             patch('botend.dashboard.api.requests.get', return_value=response_mock):
            response = self.client.get('/api/simc-result-proxy/', {'file': task.result_file})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertIn('foreign result', response.json()['content'])

    def test_task_detail_uses_workbench_dialog_and_old_modal_is_removed(self):
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        self.assertNotIn('function openViewSimcTaskModal(task)', main_js)
        self.assertIn('async function showTaskDetail(resource, id)', workbench_js)
        self.assertIn("window.openSimcWorkbenchDialog('task-detail', null)", workbench_js)
        self.assertNotIn('modal.style.display', workbench_js)

    def test_dashboard_sections_stay_inside_main_content(self):
        from bs4 import BeautifulSoup

        template = (Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        soup = BeautifulSoup(template, 'html.parser')
        main_content = soup.select_one('.main-content')

        self.assertIsNotNone(main_content)
        for section_id in ('dashboard-home', 'simc-workbench', 'tools', 'database-tables'):
            section = soup.select_one(f'#{section_id}')
            self.assertIsNotNone(section, section_id)
            self.assertIs(section.parent, main_content, section_id)

        workbench = soup.select_one('#simc-workbench')
        for section_id in ('simc-workflow', 'simc-history', 'simc-advanced'):
            section = soup.select_one(f'#{section_id}.content-section')
            self.assertIsNotNone(section, section_id)
            self.assertIs(getattr(section, 'parent', None), workbench, section_id)

    def test_simc_workbench_panels_are_grouped_by_l1_information_architecture(self):
        from bs4 import BeautifulSoup

        template = (Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        soup = BeautifulSoup(template, 'html.parser')
        expected_groups = {
            'simc-workflow': (
                'simc-workbench-import-panel', 'simc-workbench-profiles-panel',
                'simc-workbench-templates-panel', 'simc-workbench-apl-panel',
            ),
            'simc-history': ('simc-workbench-tasks-panel',),
            'simc-advanced': (
                'simc-workbench-backend-panel', 'simc-workbench-rules-panel',
            ),
        }
        for group_id, panel_ids in expected_groups.items():
            group = soup.select_one(f'#{group_id}')
            self.assertIsNotNone(group, group_id)
            for panel_id in panel_ids:
                panel = soup.select_one(f'#{panel_id}')
                self.assertIsNotNone(panel, panel_id)
                self.assertIn(group, panel.parents, panel_id)



    def test_final_execution_config_validation_summarizes_rendered_simc_without_raw_content(self):
        rendered = '''warrior="AuditActor"
spec=fury
talents=SECRET_BUILD
head=,id=123
actions=auto_attack
actions+=/bloodthirst
html=simc_task_99.html
'''

        summary = SimcMonitor.build_final_config_validation(rendered)

        self.assertEqual(summary['actor_count'], 1)
        self.assertEqual(summary['spec_count'], 1)
        self.assertEqual(summary['talents_count'], 1)
        self.assertEqual(summary['equipment_count'], 1)
        self.assertEqual(summary['action_count'], 2)
        self.assertEqual(summary['html_output_count'], 1)
        self.assertEqual(summary['placeholder_count'], 0)
        self.assertEqual(len(summary['sha256']), 64)
        self.assertNotIn('SECRET_BUILD', json.dumps(summary))

    def test_worker_persists_final_execution_validation_in_task_manifest(self):
        task = create_test_task(
            user_id=self.user.id,
            name='Worker audit',
            simc_profile_id=0,
            task_type=1,
            current_status=1,
            ext=json.dumps({'spec': 'fury', 'override_action_list': 'actions=SECRET'}),
        )

        SimcMonitor.persist_final_config_validation(task, 'warrior="A"\nspec=fury\nactions=auto_attack\nhtml=x.html')

        task.refresh_from_db()
        manifest = json.loads(task.ext)
        self.assertEqual(manifest['final_config_validation']['actor_count'], 1)
        self.assertNotIn('SECRET', json.dumps(manifest['final_config_validation']))
        self.assertEqual(manifest['override_action_list'], 'actions=SECRET')

    def test_task_preview_returns_persisted_final_execution_validation(self):
        validation = {
            'char_count': 12000, 'line_count': 280, 'sha256': 'a' * 64,
            'actor_count': 1, 'spec_count': 1, 'talents_count': 1,
            'equipment_count': 14, 'action_count': 112,
            'html_output_count': 1, 'placeholder_count': 0,
        }
        task = create_test_task(
            user_id=self.user.id,
            name='Validated task',
            simc_profile_id=0,
            task_type=1,
            current_status=2,
            ext=json.dumps({'spec': 'fury', 'final_config_validation': validation}),
        )

        response = self.client.get(f'/api/simc-task/preview/?task_id={task.id}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['final_config_validation'], validation)

    @override_settings(SIMC_APL_CURRENT_IDENTITY=('test-revision', 'test-build'))
    @patch('botend.services.simc_task_service.validate_apl_for_profile')
    def test_rerun_creates_pending_task_without_mutating_completed_manifest_task(self, validate_apl):
        """Test rerun for reference-based tasks creates a new pending task."""
        from botend.models import SimcResourceVersion, SimcProfile, SimcContentTemplate, SimcApl
        import hashlib

        validate_apl.return_value = {
            'valid': True,
            'content_hash': hashlib.sha256(b'actions=/bloodthirst').hexdigest(),
            'revision': 'test-revision',
            'game_build': 'test-build',
        }

        # Create live resources with a different spec to avoid conflicts with setUp
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Completed Profile',
            spec='warrior_arms',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Snapshot"\nspec=arms\nhead=,id=212048',
        )
        template = SimcContentTemplate.objects.create(
            source='user',
            spec='warrior_arms',
            name='Rerun Test Template',
            content='warrior="T"\n{player_config}\n',
        )
        apl = SimcApl.objects.create(
            name='Rerun Test APL',
            spec='warrior_arms',
            content='actions=/bloodthirst',
            source='user',
            is_active=True,
            is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(b'actions=/bloodthirst').hexdigest(),
            validation_revision='test-revision',
            validation_game_build='test-build',
        )

        # Create versions
        profile_payload = {
            "player_config_mode": "manual_equipment",
            "player_equipment": "warrior=\"Snapshot\"\nspec=arms\nhead=,id=212048",
        }
        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=profile.id,
            content_hash=hashlib.sha256(json.dumps(profile_payload, sort_keys=True).encode()).hexdigest(),
            payload=profile_payload,
        )
        template_payload = {'content': 'warrior="T"\n{player_config}\n'}
        template_version = SimcResourceVersion.objects.create(
            resource_type='template', resource_id=template.id,
            content_hash=hashlib.sha256(json.dumps(template_payload, sort_keys=True).encode()).hexdigest(),
            payload=template_payload,
        )
        apl_payload = {'content': 'actions=/bloodthirst'}
        apl_version = SimcResourceVersion.objects.create(
            resource_type='apl', resource_id=apl.id,
            content_hash=hashlib.sha256(json.dumps(apl_payload, sort_keys=True).encode()).hexdigest(),
            payload=apl_payload,
        )

        # Create completed reference task
        original = create_test_task(
            user_id=self.user.id,
            name='Completed reference task',
            task_type=1,
            simc_profile_id=0,
            profile_id=profile.id,
            profile_version_id=profile_version.id,
            template_id=template.id,
            template_version_id=template_version.id,
            apl_id=apl.id,
            apl_version_id=apl_version.id,
            current_status=1,
            result_file='simc_task_completed.html',
        )

        response = self.client.patch(
            '/api/simc-task/',
            data=json.dumps({'id': original.id, 'action': 'rerun'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        rerun_id = payload['data']['id']
        self.assertNotEqual(rerun_id, original.id)
        self.assertEqual(SimcTask.objects.count(), 2)

        original.refresh_from_db()
        self.assertEqual(original.current_status, 1)
        self.assertEqual(original.result_file, 'simc_task_completed.html')

        rerun = SimcTask.objects.get(id=rerun_id)
        self.assertEqual(rerun.current_status, 0)
        self.assertRegex(rerun.result_file, r'^[0-9a-f]{32}\.html$')
        self.assertEqual(rerun.profile_id, profile.id)
        self.assertEqual(rerun.profile_version_id, profile_version.id)
        self.assertEqual(rerun.template_id, template.id)
        self.assertEqual(rerun.template_version_id, template_version.id)
        self.assertEqual(rerun.apl_id, apl.id)
        self.assertEqual(rerun.apl_version_id, apl_version.id)

    def test_direct_attribute_task_rejects_non_50_step(self):
        """Old task_type=2 is rejected; test confirms proper error message."""
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Bad direct attribute step',
                'task_type': 2,
                'player_import_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=SNAPSHOT_BUILD\nhead=,id=212048\nmain_hand=,id=222222',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 25,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload['success'], payload)
        self.assertIn('task_type', payload['error'])









    def test_apply_template_builds_battlenet_armory_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'deathknight="LMonitor_Base"\nspec={spec}\nfight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'spec': 'fury',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertNotIn('Bloodmast_r', rendered)
        self.assertNotIn('deathknight="LMonitor_Base"', rendered)
        self.assertNotIn('\nspec=fury', rendered)
        self.assertIn('armory=eu,Kazzak,Bloodmastêr', rendered)
        self.assertIn('actions=auto_attack', rendered)

    def test_apply_template_battlenet_does_not_override_imported_player(self):
        monitor = object.__new__(SimcMonitor)
        template = '\n'.join([
            'deathknight="LMonitor_Base"',
            'source=default',
            'spec={spec}',
            'level=80',
            'race=mechagnome',
            'role=attack',
            'position=back',
            'fight_style={fight_style}',
            'max_time={time}',
            'desired_targets={target_count}',
            'talents={talent}',
            'potion=tempered_potion_3',
            'gear_crit_rating={gear_crit}',
            '{player_config}',
            '{action_list}',
        ])

        rendered = monitor.apply_template(template, {
            'player_import_mode': 'battlenet',
            'battlenet_region': 'eu',
            'battlenet_realm': 'Kazzak',
            'battlenet_character': 'Bloodmastêr',
            'spec': 'blood',
            'fight_style': 'Patchwerk',
            'time': 300,
            'target_count': 1,
            'override_action_list': 'actions=auto_attack',
        })

        self.assertIn('armory=eu,Kazzak,Bloodmastêr', rendered)
        self.assertIn('fight_style=Patchwerk', rendered)
        self.assertIn('max_time=300', rendered)
        self.assertIn('desired_targets=1', rendered)
        self.assertIn('actions=auto_attack', rendered)
        for player_option in (
            'deathknight=', 'source=', 'spec=', 'level=', 'race=', 'role=',
            'position=', 'talents=', 'potion=', 'gear_crit_rating=',
        ):
            self.assertNotIn(player_option, rendered)

    def test_apply_template_manual_equipment_replaces_template_actor_instead_of_creating_two_players(self):
        monitor = object.__new__(SimcMonitor)
        template = 'warrior="LMonitor_Base"\nspec=fury\ntalents=TEMPLATE\n{player_config}\n{action_list}'
        player = 'warrior="Real_Player"\nspec=fury\ntalents=CANDIDATE\nhead=,id=212048'
        rendered = monitor.apply_template(template, {
            'spec': 'fury', 'talent': 'CANDIDATE',
            'player_import_mode': 'manual_equipment',
            'player_equipment': player,
            'override_action_list': 'actions=auto_attack',
        })
        self.assertNotIn('warrior="LMonitor_Base"', rendered)
        self.assertEqual(rendered.count('warrior="Real_Player"'), 1)
        self.assertEqual(rendered.count('\nspec=fury'), 1)
        self.assertEqual(rendered.count('\ntalents=CANDIDATE'), 1)
        self.assertNotIn('talents=TEMPLATE', rendered)

    def test_standard_raid_buff_migration_updates_all_base_templates(self):
        self.default_apl.is_active = False
        self.default_apl.save()
        migration = importlib.import_module(
            'botend.migrations.0103_enable_standard_simc_raid_buffs'
        )
        first = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='default',
            content='fight_style=Patchwerk\noptimal_raid=0\n{player_config}',
        )
        second = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='fury',
            content='optimal_raid=0\noverride.battle_shout=1',
        )
        apl = SimcApl.objects.create(
            name='Migration APL',
            spec='warrior_fury',
            content='# optimal_raid=0 must not alter APL content',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
        )
        class HistoricalTemplateManager:
            @staticmethod
            def filter(**kwargs):
                # Migration 0103 ran while template_type still existed; all rows in
                # the current physical table are base templates.
                kwargs.pop('template_type', None)
                return SimcContentTemplate.objects.filter(**kwargs)

        historical_model = SimpleNamespace(objects=HistoricalTemplateManager())
        apps = SimpleNamespace(get_model=lambda *args: historical_model)

        migration.enable_standard_raid_buffs(apps, None)

        first.refresh_from_db()
        second.refresh_from_db()
        apl.refresh_from_db()
        self.assertIn('optimal_raid=1', first.content)
        self.assertIn('optimal_raid=1', second.content)
        self.assertNotIn('optimal_raid=0', first.content)
        self.assertEqual(apl.content, '# optimal_raid=0 must not alter APL content')

    def test_apply_template_manual_equipment_preserves_template_runtime_options(self):
        monitor = object.__new__(SimcMonitor)
        template = '\n'.join([
            'warrior="LMonitor_Base"',
            'spec={spec}',
            'fight_style={fight_style}',
            'max_time={time}',
            'desired_targets={target_count}',
            'optimal_raid=0',
            'override.battle_shout=1',
            'potion=tempered_potion_3',
            'shoulders=TEMPLATE_SHOULDERS,id=1',
            'wrists=TEMPLATE_WRISTS,id=2',
            '{player_config}',
            '{action_list}',
        ])
        player = '\n'.join([
            'warrior="Real_Player"',
            'spec=fury',
            'talents=CANDIDATE',
            'head=,id=212048',
            'shoulders=,id=212050',
            'wrists=,id=211999',
            'main_hand=,id=224638',
        ])

        rendered = monitor.apply_template(template, {
            'spec': 'fury', 'talent': 'CANDIDATE',
            'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
            'player_import_mode': 'manual_equipment',
            'player_equipment': player,
            'override_action_list': 'actions=auto_attack',
        })

        self.assertEqual(rendered.count('warrior="Real_Player"'), 1)
        self.assertNotIn('warrior="LMonitor_Base"', rendered)
        self.assertIn('fight_style=Patchwerk', rendered)
        self.assertIn('max_time=300', rendered)
        self.assertIn('desired_targets=1', rendered)
        self.assertIn('optimal_raid=0', rendered)
        self.assertIn('override.battle_shout=1', rendered)
        self.assertIn('potion=tempered_potion_3', rendered)
        self.assertNotIn('TEMPLATE_SHOULDERS', rendered)
        self.assertNotIn('TEMPLATE_WRISTS', rendered)
        self.assertEqual(rendered.count('shoulders=,id=212050'), 1)
        self.assertEqual(rendered.count('wrists=,id=211999'), 1)

    def test_apply_template_manual_equipment_removes_template_player_fields_after_placeholder(self):
        monitor = object.__new__(SimcMonitor)
        template = '\n'.join([
            '{player_config}',
            'spec={spec}',
            'talents=TEMPLATE',
            'shoulders=TEMPLATE_SHOULDERS',
            'fight_style={fight_style}',
            '{action_list}',
            'html={result_file}',
        ])
        rendered = monitor.apply_template(template, {
            'spec': 'fury',
            'fight_style': 'Patchwerk',
            'player_import_mode': 'manual_equipment',
            'player_equipment': '\n'.join([
                'warrior="Real_Player"',
                'spec=fury',
                'talents=CANDIDATE',
                'shoulders=,id=212050',
            ]),
            'override_action_list': 'actions=auto_attack',
            'result_file': 'simc_task_101.html',
        })
        self.assertNotIn('talents=TEMPLATE', rendered)
        self.assertNotIn('TEMPLATE_SHOULDERS', rendered)
        self.assertEqual(rendered.count('\nspec=fury'), 1)
        self.assertIn('fight_style=Patchwerk', rendered)

    def test_apply_template_manual_equipment_requires_exactly_one_player_placeholder(self):
        monitor = object.__new__(SimcMonitor)
        config = {
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Real_Player"\nspec=fury\ntalents=CANDIDATE',
            'override_action_list': 'actions=auto_attack',
        }

        with self.assertRaisesRegex(ValueError, 'player_config.*恰好一个'):
            monitor.apply_template('warrior="Template"\nspec={spec}\n{action_list}', config)
        with self.assertRaisesRegex(ValueError, 'player_config.*恰好一个'):
            monitor.apply_template('{player_config}\n{player_config}\n{action_list}', config)

    def test_apply_template_inserts_manual_equipment_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'fight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('talents=TEST', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertIn('actions=auto_attack', rendered)
    def test_apply_template_manual_equipment_truncates_exported_alternative_sections(self):
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'fight_style=Patchwerk\n{player_config}\n{action_list}',
            {
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'warrior="Real"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222\n### Gear from Bags\nhead=,id=299001',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('head=,id=212048', rendered)
        self.assertNotIn('299001', rendered)

    def test_apply_template_inserts_attribute_only_frozen_player_and_rating_overrides(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'spec={spec}\n{player_config}\n{gear_crit}\n{gear_haste}\n{gear_mastery}\n{gear_versatility}\n{action_list}',
            {
                'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'ATTRIBUTE_BUILD',
                'gear_strength': 5000,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('talents=ATTRIBUTE_BUILD', rendered)
        self.assertNotRegex(rendered, r'(?m)^\s*gear_strength\s*=')
        self.assertIn('crit_rating=1000', rendered)
        self.assertIn('haste_rating=2000', rendered)
        self.assertIn('mastery_rating=3000', rendered)
        self.assertIn('versatility_rating=4000', rendered)
        self.assertNotIn('{gear_', rendered)
        self.assertNotIn('armory=', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertIn('actions=auto_attack', rendered)


@override_settings(SIMC_APL_CURRENT_IDENTITY=('test-revision', 'test-build'))
class SimcPlayerConfigDetailTests(TestCase):
    """玩家详情只解析当前输入与本地快照，不渲染完整 SimC 执行配置。"""

    def setUp(self):
        self.backend = get_test_backend()
        self.user = User.objects.create_user(username='player_detail_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}\n{simulation_options}\n{stat_overrides}\n{output_options}',
            is_active=True,
        )
        self.default_apl = SimcApl.objects.create(
            name='Player Detail APL',
            spec='warrior_fury',
            content='actions=/auto_attack\nactions+=/bloodthirst',
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
            is_active=True,
            is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(
                b'actions=/auto_attack\nactions+=/bloodthirst').hexdigest(),
            validation_revision=TEST_SIMC_REVISION, validation_game_build=TEST_WOW_BUILD,
        )
        self.apl_validation = patch(
            'botend.services.simc_task_service.validate_apl_for_profile',
            return_value={'valid': True,
                          'content_hash': hashlib.sha256(
                              b'actions=/auto_attack\nactions+=/bloodthirst').hexdigest(),
                          'revision': TEST_SIMC_REVISION, 'game_build': TEST_WOW_BUILD},
        )
        self.apl_validation.start()
        self.addCleanup(self.apl_validation.stop)

    def _create_profile(self, name, player_block):
        return SimcProfile.objects.create(
            user_id=self.user.id,
            name=name,
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment=player_block,
            talent='ACTIVE_BUILD',
            is_active=True,
        )

    def test_player_config_detail_returns_structured_manual_player_detail_with_items_and_stats(self):
        WowItemSnapshot.objects.create(item_id=212048, name='Helm of Tests', name_zh='测试头盔', icon='inv_helmet_01')
        WowItemSnapshot.objects.create(item_id=71543, name='Swift Enchant', name_zh='迅捷附魔')
        WowItemSnapshot.objects.create(item_id=213479, name='Test Gem', name_zh='测试宝石')
        from botend.models import SimcSecondaryStatRule
        SimcSecondaryStatRule.objects.update_or_create(
            class_name='warrior',
            defaults={
                'crit_per_percent': 46, 'haste_per_percent': 44,
                'mastery_per_percent': 46, 'versatility_per_percent': 54,
            },
        )
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': '\n'.join([
                    'warrior="Previewer"',
                    'level=80',
                    'race=orc',
                    'region=cn',
                    'server=死亡之翼',
                    'spec=fury',
                    'talents=BUILDCODE',
                    'head=,id=212048,ilevel=639,enchant_id=71543,gems=213479/213480',
                    'main_hand=,id=224638,ilevel=646',
                    'crit_rating=10730',
                    'haste_rating=18641',
                    'mastery_rating=21785',
                    'versatility_rating=6757',
                ]),
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']
        self.assertEqual(detail['source']['type'], 'manual_equipment')
        self.assertEqual(detail['identity']['name'], 'Previewer')
        self.assertEqual(detail['identity']['race'], 'orc')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['talents']['build_code'], 'BUILDCODE')
        self.assertEqual(detail['equipment'][0]['slot'], 'head')
        self.assertEqual(detail['equipment'][0]['display_name'], '测试头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 639)
        self.assertEqual(detail['equipment'][0]['enchant']['display_name'], '迅捷附魔')
        self.assertEqual(detail['equipment'][0]['gems'][0]['display_name'], '测试宝石')
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 10730)
        self.assertAlmostEqual(detail['stats']['secondary']['crit']['percent'], 233.26, places=2)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_get_saved_profile_returns_raw_and_structured_equipment_detail(self):
        profile = self._create_profile('Saved profile', 'warrior="Saved"\nspec=fury\nhead=,id=212048,ilevel=639')
        profile.use_ptr = True
        profile.save(update_fields=['use_ptr'])
        response = self.client.get(f'/api/simc-player-config-detail/?profile_id={profile.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['profile']['id'], profile.id)
        self.assertEqual(data['profile']['raw_player_equipment'], profile.player_equipment)
        self.assertEqual(data['profile']['canonical_spec'], 'warrior_fury')
        self.assertEqual(data['profile']['talent'], 'ACTIVE_BUILD')
        self.assertIs(data['profile']['use_ptr'], True)
        self.assertIn('talent_versions', data)
        self.assertEqual(data['equipment'][0]['slot'], 'head')
        self.assertEqual(data['equipment'][0]['item_id'], 212048)


    def test_player_config_detail_exposes_only_parsed_comparison_candidates(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
trinket1=,id=111,ilevel=639
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
### Gear from Bags
# Candidate Trinket (645)
trinket1=,id=222,ilevel=645
### Weekly Reward Choices
# Candidate Ring (646)
finger1=,id=333,ilevel=646
'''
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury', 'player_config_mode': 'manual_equipment',
                'player_equipment': player_block,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        candidates = payload['data']['comparison_candidates']
        self.assertEqual(candidates['max_selectable'], 7)
        self.assertEqual(
            [(row['slot'], row['item_id'], row['source']) for row in candidates['gear']],
            [('trinket1', 222, 'bags'), ('finger1', 333, 'weekly_reward')],
        )
        self.assertEqual(candidates['talents'], [{'name': 'Cleave', 'talent': 'CLEAVE_BUILD', 'source': 'saved_loadout'}])

    def test_talent_candidate_batch_replaces_player_block_talent_before_execution(self):
        """Batch creation now requires base_template_id and apl_id."""
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
trinket1=,id=111,ilevel=639
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
'''
        profile = self._create_profile('Talent Replacement Test', player_block)
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 天赋对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{'talent': 'CLEAVE_BUILD'}],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertEqual(SimcTask.objects.count(), 1)
        task = SimcTask.objects.get()
        self.assertEqual(task.simulation_runs.count(), 0)
        self.assertEqual(len(task.mode_params['initial_candidates']), 2)
        self.assertIsNotNone(task.profile_id)
        self.assertIsNotNone(task.profile_version_id)
        self.assertIsNotNone(task.template_id)
        self.assertIsNotNone(task.template_version_id)
        self.assertIsNotNone(task.apl_id)
        self.assertIsNotNone(task.apl_version_id)

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_talent_candidate_batch_accepts_battlenet_source_and_freezes_selected_loadouts(self, preflight):
        player_snapshot = (
            'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=ACTIVE_BUILD\n'
            'head=,id=212048\nmain_hand=,id=222222'
        )
        preflight.return_value = {
            'simc_ready': True, 'warnings': [],
            'simc_config': {
                'player_config_mode': 'battlenet', 'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak', 'battlenet_character': 'Batcher',
                'spec': 'fury', 'talent': 'ACTIVE_BUILD',
                'player_equipment': player_snapshot,
                'gear_strength': 10000, 'gear_crit': 1000, 'gear_haste': 2000,
                'gear_mastery': 3000, 'gear_versatility': 4000,
            },
            'comparison_candidates': {
                'default_talent': {
                    'name': '默认天赋', 'talent': 'ACTIVE_BUILD', 'source': 'battlenet_active',
                },
                'talents': [
                    {'name': '团本', 'talent': 'RAID_BUILD', 'source': 'battlenet_loadout'},
                    {'name': '大秘境', 'talent': 'MPLUS_BUILD', 'source': 'battlenet_loadout'},
                ],
                'gear': [],
            },
        }

        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury Battle.net 天赋对比',
            'spec': 'warrior_fury',
            'player_source': {
                'type': 'battlenet', 'region': 'eu', 'realm': 'Kazzak', 'character': 'Batcher',
            },
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'include_base': True,
            'candidates': [
                {'name': '团本', 'talent': 'RAID_BUILD', 'source': 'battlenet_loadout'},
                {'name': '大秘境', 'talent': 'MPLUS_BUILD', 'source': 'battlenet_loadout'},
            ],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['task_id'])
        profile = task.profile_version.payload
        self.assertEqual(profile['player_config_mode'], 'manual_equipment')
        self.assertEqual(profile['player_equipment'], player_snapshot)
        self.assertEqual(task.simulation_runs.count(), 0)
        frozen = task.mode_params['initial_candidates']
        self.assertEqual(len(frozen), 3)
        self.assertEqual(
            [row['candidate_params'].get('talent_override') for row in frozen],
            [None, 'RAID_BUILD', 'MPLUS_BUILD'],
        )

    def test_talent_candidate_batch_accepts_named_manual_build_and_freezes_report_metadata(self):
        profile = self._create_profile('Manual talent Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 手工天赋对比',
            'include_base': False,
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{
                'name': '手工单体方案', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
            }],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get()
        self.assertEqual(task.simulation_runs.count(), 0)
        candidate = task.mode_params['initial_candidates'][0]
        self.assertEqual(candidate['candidate_label'], '手工单体方案')
        self.assertEqual(candidate['candidate_params']['candidate_type'], 'talent_override')
        self.assertEqual(candidate['candidate_params']['talent_override'], 'MANUAL_TALENT_BUILD')
        self.assertEqual(candidate['candidate_params']['talent_candidate'], {
            'name': '手工单体方案', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
        })

    def test_talent_candidate_batch_rejects_manual_build_without_name(self):
        profile = self._create_profile('Invalid manual talent Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 手工天赋对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{'name': '', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual'}],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('方案名称', response.json()['error'])

    def test_gear_candidate_batch_rejects_slot_not_in_baseline_block(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
### Gear from Bags
# Candidate ring (645)
finger1=,id=222,ilevel=645
'''
        profile = self._create_profile('Gear missing-slot Test', player_block)
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{'slot': 'finger1', 'item_id': 222, 'source': 'bags'}],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('未包含可替换的装备槽位', response.json()['error'])

    def test_candidate_batch_rejects_duplicate_candidates(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
### Gear from Bags
# Candidate helm (645)
head=,id=222,ilevel=645
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
'''
        profile = self._create_profile('Duplicate candidate Test', player_block)
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
            ],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('不可重复选择', response.json()['error'])

    def test_gear_candidate_batch_accepts_valid_manual_slot_override(self):
        profile = self._create_profile('Manual candidate Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 手工装备对比',
            'include_base': False,
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{
                'slot': 'head', 'item_id': 444, 'source': 'manual',
                'raw_value': ',id=444,ilevel=650', 'name': '手工候选头盔',
            }],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertEqual(SimcTask.objects.count(), 1)
        task = SimcTask.objects.get()
        self.assertEqual(task.simulation_runs.count(), 0)
        candidate = task.mode_params['initial_candidates'][0]
        self.assertEqual(candidate['candidate_params']['gear_swap'], {
            'slot': 'head', 'raw_value': ',id=444,ilevel=650',
            'item_id': 444, 'source': 'manual',
        })

    def test_gear_candidate_batch_accepts_full_simc_gear_line(self):
        profile = self._create_profile('Full manual candidate', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 完整装备行对比',
            'include_base': False,
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{
                'slot': 'head', 'item_id': 444, 'source': 'manual',
                'raw_value': 'head=手工候选头盔,id=444,ilevel=650',
            }],
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get()
        swap = task.mode_params['initial_candidates'][0]['candidate_params']['gear_swap']
        self.assertEqual(swap['raw_value'], '手工候选头盔,id=444,ilevel=650')
        rendered = SimcMonitor.apply_candidate_overrides(
            {'player_equipment': profile.player_equipment},
            {'candidate_type': 'gear_swap', 'gear_swap': swap},
        )
        self.assertIn('head=手工候选头盔,id=444,ilevel=650', rendered['player_equipment'])
        self.assertNotIn('head=head=', rendered['player_equipment'])

    def test_gear_candidate_batch_rejects_manual_line_for_another_slot(self):
        profile = self._create_profile('Invalid manual candidate Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 非法手工装备对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{
                'slot': 'head', 'item_id': 444, 'source': 'manual',
                'raw_value': 'neck=,id=444,ilevel=650',
            }],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('槽位', response.json()['error'])

    def test_real_simc_export_keeps_main_gear_names_and_excludes_bag_choices(self):
        config = '''# 炎色雷灬 - Fury - 2026-07-10 02:37 - CN/死亡之翼
warrior="炎色雷灬"
level=90
race=orc
region=cn
server=死亡之翼
role=attack
professions=enchanting=100/jewelcrafting=100
spec=fury
talents=ACTIVE_BUILD
# Saved Loadout: 团本屠戮
# talents=SAVED_BUILD
omnium_talents=136817:1/136819:1
# 终夜者的獠牙头盔 (289)
head=,id=249952,enchant_id=8017,gem_id=240892,bonus_id=6652/13534
# 腐沼的孢子之心 (298)
neck=,id=268291,gem_id=240983,bonus_id=6652/13668
# 信徒的流丝罩袍 (285)
back=,id=239656,bonus_id=12214/13667,content_tuning=3615,crafted_stats=32/36,crafting_quality=5
# 旋风虚空裂斧 (298)
main_hand=,id=251117,enchant_id=8041,bonus_id=13440/6652
### Gear from Bags
# 盘绕恶意丝带 (285)
# neck=,id=249337,bonus_id=6652/13668
'''
        detail = parse_manual_player_config(config, 'fury')

        self.assertEqual(detail['identity']['name'], '炎色雷灬')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['identity']['role'], 'attack')
        self.assertEqual(detail['identity']['professions'], {'enchanting': 100, 'jewelcrafting': 100})
        self.assertEqual(detail['talents']['build_code'], 'ACTIVE_BUILD')
        self.assertEqual(detail['talents']['saved_loadouts'], [{'name': '团本屠戮', 'build_code': 'SAVED_BUILD'}])
        self.assertEqual(len(detail['equipment']), 4)
        self.assertEqual(detail['equipment'][0]['display_name'], '终夜者的獠牙头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 289)
        self.assertEqual(detail['equipment'][0]['gems'][0]['id'], 240892)
        self.assertEqual(detail['equipment'][2]['crafted_stats'], ['精通', '全能'])
        self.assertEqual(detail['equipment'][2]['crafting_quality'], 5)
        self.assertEqual(detail['omnium_talents'], [{'id': 136817, 'rank': 1}, {'id': 136819, 'rank': 1}])

    def test_player_config_detail_returns_battlenet_identity_and_explicit_missing_detail(self):
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']
        self.assertEqual(detail['source']['type'], 'battlenet')
        self.assertEqual(detail['identity']['region'], 'eu')
        self.assertEqual(detail['identity']['realm'], 'Kazzak')
        self.assertEqual(detail['identity']['name'], 'Bloodmastêr')
        self.assertEqual(detail['equipment'], [])
        self.assertTrue(detail['missing_fields'])
        self.assertIn('未保存角色装备快照', detail['missing_fields'][0])


    def test_attribute_only_profile_load_contract_keeps_equipment_empty(self):
        """工作台加载历史属性配置时，属性只能进入专用字段，不能污染隐藏装备框。"""
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Legacy workbench load contract',
            spec='fury',
            player_config_mode='battlenet',  # 新字段迁移时的错误历史默认值。
            player_equipment='',
            battlenet_region='',
            battlenet_realm='',
            battlenet_character='',
            talent='WORKBENCH_BUILD',
            gear_crit=401,
            gear_haste=1100,
            gear_mastery=1140,
            gear_versatility=100,
        )

        response = self.client.get(f'/api/simc-profile/{profile.id}/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['player_config_mode'], 'attribute_only')
        self.assertEqual(payload['talent'], 'WORKBENCH_BUILD')
        self.assertEqual(payload['player_equipment'], '')
        self.assertFalse(payload['battlenet_region'])
        self.assertFalse(payload['battlenet_realm'])


class SimcBattlenetPreflightTests(TestCase):
    """Battle.net 提交前预检必须真实获取角色信息，而不是只回显 armory 三元组。"""

    @patch('botend.services.battlenet_preflight.TalentBuildCodeService.encode_build_code_from_nodes')
    @patch('botend.services.battlenet_preflight.TalentMetadataProvider.get_decoder_node_list')
    def test_saved_loadout_reencodes_missing_granted_hero_root(
        self, get_decoder_nodes, encode_build_code,
    ):
        from botend.services.battlenet_preflight import _canonicalize_talent_loadout

        get_decoder_nodes.return_value = [
            {
                'talent_id': 100, 'tree_type': 'hero', 'db2_subtree_id': 60,
                'parents': [], 'max_points': 1,
            },
            {
                'talent_id': 101, 'tree_type': 'hero', 'db2_subtree_id': 60,
                'parents': [100], 'max_points': 1,
            },
            {
                'talent_id': 200, 'tree_type': 'hero', 'db2_subtree_id': 62,
                'parents': [], 'max_points': 1,
            },
        ]
        encode_build_code.return_value = 'CANONICAL_WITH_GRANTED_ROOT'
        loadout = {
            'talent_loadout_code': 'RAW_SAVED_CODE_WITHOUT_ROOT',
            'selected_class_talents': [{'id': 10, 'rank': 1}],
            'selected_spec_talents': [{'id': 20, 'rank': 2}],
            'selected_hero_talents': [{'id': 101, 'rank': 1}],
        }

        result = _canonicalize_talent_loadout(loadout, class_name='warrior', spec_name='arms')

        self.assertEqual(result, 'CANONICAL_WITH_GRANTED_ROOT')
        selected_nodes = encode_build_code.call_args.args[0]
        self.assertIn(
            {
                'talent_id': 100, 'tree_type': 'hero', 'db2_subtree_id': 60,
                'points': 1, 'selected': True, 'purchased': False,
            },
            selected_nodes,
        )
        self.assertNotIn(200, [node.get('talent_id') for node in selected_nodes])
        self.assertEqual(
            encode_build_code.call_args.kwargs['reference_build_code'],
            'RAW_SAVED_CODE_WITHOUT_ROOT',
        )

    @patch('botend.services.battlenet_preflight.TalentMetadataProvider.get_decoder_node_list')
    def test_saved_loadout_preserves_reference_choice_state_when_adding_granted_root(
        self, get_decoder_nodes,
    ):
        from botend.services.battlenet_preflight import _canonicalize_talent_loadout
        from botend.wow.talents.build_code import (
            TalentBuildCodeDecoder,
            _ImportBitWriter,
        )

        decoder_nodes = [
            {
                'talent_id': 99853, 'node_id': 123390,
                'tree_type': 'hero_anchor', 'max_points': 1,
                'choice_options': [
                    {'talent_id': 99853, 'node_id': 123390, 'max_points': 1},
                    {'talent_id': 99853, 'node_id': 123393, 'max_points': 1},
                ],
            },
            {
                'talent_id': 100000, 'node_id': 130000,
                'tree_type': 'hero', 'db2_subtree_id': 60,
                'parents': [], 'max_points': 1,
            },
            {
                'talent_id': 100001, 'node_id': 130001,
                'tree_type': 'hero', 'db2_subtree_id': 60,
                'parents': [130000], 'max_points': 1,
            },
        ]
        get_decoder_nodes.return_value = decoder_nodes

        writer = _ImportBitWriter()
        writer.write(0, TalentBuildCodeDecoder.HEADER_VERSION_BITS)
        writer.write(0, TalentBuildCodeDecoder.SPEC_ID_BITS)
        for _ in range(16):
            writer.write(0, 8)
        writer.write(1, 1)  # hero selector selected
        writer.write(1, 1)  # purchased
        writer.write(0, 1)  # full rank
        writer.write(1, 1)  # choice marker
        writer.write(1, 2)  # preserve second selector entry
        writer.write(0, 1)  # granted hero root omitted by Saved Loadout
        writer.write(1, 1)  # paid hero child selected
        writer.write(1, 1)  # purchased
        writer.write(0, 1)  # full rank
        writer.write(0, 1)  # non-choice
        reference = writer.to_string()

        result = _canonicalize_talent_loadout({
            'talent_loadout_code': reference,
            'selected_hero_talents': [
                {'id': 99853, 'rank': 1, 'default_points': 1},
                {'id': 100001, 'rank': 1},
            ],
        }, class_name='warrior', spec_name='arms')
        decoded = TalentBuildCodeDecoder.decode_node_states(result, decoder_nodes)

        self.assertEqual(decoded['hero_anchor:123390']['choice_selection'], 1)
        self.assertTrue(decoded['hero_anchor:123390']['is_choice_node'])
        self.assertTrue(decoded['hero_anchor:123390']['purchased'])
        self.assertEqual(decoded['hero:130000']['points'], 1)
        self.assertFalse(decoded['hero:130000']['purchased'])
        self.assertEqual(decoded['hero:130001']['points'], 1)

    @patch('botend.services.battlenet_preflight.TalentMetadataProvider.get_decoder_node_list')
    def test_saved_loadout_drops_reference_choice_marker_for_canonical_apex_pool(
        self, get_decoder_nodes,
    ):
        from botend.services.battlenet_preflight import _canonicalize_talent_loadout
        from botend.wow.talents.build_code import (
            TalentBuildCodeDecoder,
            _ImportBitWriter,
        )

        decoder_nodes = [{
            'talent_id': 110407,
            'node_id': 136987,
            'tree_type': 'hero_anchor',
            'max_points': 4,
            'is_choice_node': False,
            'is_apex_talent': True,
            'apex_entries': [
                {'talent_id': 110407, 'node_id': 136987, 'max_points': 1},
                {'talent_id': 110407, 'node_id': 136988, 'max_points': 2},
                {'talent_id': 110407, 'node_id': 136989, 'max_points': 1},
            ],
            'choice_options': [
                {'talent_id': 110407, 'node_id': 136987, 'max_points': 1},
                {'talent_id': 110407, 'node_id': 136988, 'max_points': 2},
                {'talent_id': 110407, 'node_id': 136989, 'max_points': 1},
            ],
        }]
        get_decoder_nodes.return_value = decoder_nodes

        writer = _ImportBitWriter()
        writer.write(0, TalentBuildCodeDecoder.HEADER_VERSION_BITS)
        writer.write(0, TalentBuildCodeDecoder.SPEC_ID_BITS)
        for _ in range(16):
            writer.write(0, 8)
        writer.write(1, 1)  # selected
        writer.write(1, 1)  # purchased
        writer.write(1, 1)  # partially ranked
        writer.write(1, TalentBuildCodeDecoder.RANKS_PURCHASED_BITS)
        writer.write(1, 1)  # stale/invalid choice marker from Battle.net reference
        writer.write(1, 2)
        reference = writer.to_string()

        result = _canonicalize_talent_loadout({
            'talent_loadout_code': reference,
            'selected_spec_talents': [{'id': 110407, 'rank': 1}],
        }, class_name='warrior', spec_name='arms')
        decoded = TalentBuildCodeDecoder.decode_node_states(result, decoder_nodes)

        self.assertEqual(decoded['hero_anchor:136987']['points'], 1)
        self.assertFalse(decoded['hero_anchor:136987']['is_choice_node'])
        self.assertEqual(decoded['hero_anchor:136987']['choice_selection'], 0)

    def setUp(self):
        self.user = User.objects.create_user(username='battlenet_preflight_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_top_players_returns_active_season_spec_top10_for_battlenet_picker(self):
        inactive = SeasonMeta.objects.create(
            season_key='old-season', season_name='旧赛季', is_active=False,
            mplus_zone_id=1, raid_zone_id=1,
        )
        active = SeasonMeta.objects.create(
            season_key='current-season', season_name='当前赛季', is_active=True,
            mplus_zone_id=2, raid_zone_id=2,
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=inactive.id, class_name='Warrior', spec_name='Fury', rank=1,
            score=9999, region='eu', realm='Old Realm', character_name='Oldplayer',
        )
        for index in range(22):
            PlayerSpecTopPlayer.objects.create(
                season_id=active.id,
                class_name='Warrior' if index < 21 else 'Mage',
                spec_name='Protection' if index == 20 else ('Fury' if index % 2 == 0 else 'Arms'),
                rank=index + 1,
                score=5000 - index,
                region='EU' if index % 2 == 0 or index == 20 else 'us',
                realm='Realm 0' if index == 20 else f'Realm {index}',
                character_name='Player0' if index == 20 else f'Player{index}',
            )

        response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_fury')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['spec'], 'warrior_fury')
        self.assertEqual(payload['season']['id'], active.id)
        self.assertEqual(len(payload['data']), 10)
        self.assertEqual({row['spec'] for row in payload['data']}, {'fury'})
        identities = [(row['region'], row['realm'].casefold(), row['character'].casefold()) for row in payload['data']]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(payload['data'][0], {
            'id': payload['data'][0]['id'],
            'rank': 1,
            'score': 5000.0,
            'spec': 'fury',
            'region': 'eu',
            'realm': 'Realm 0',
            'character': 'Player0',
            'label': 'Player0 · Realm 0 · EU · 狂怒',
        })
        self.assertNotIn('Oldplayer', [row['character'] for row in payload['data']])

    def test_top_players_query_does_not_load_large_character_payload_fields(self):
        active = SeasonMeta.objects.create(
            season_key='current-season-lightweight-top10', season_name='当前赛季', is_active=True,
            mplus_zone_id=2, raid_zone_id=2,
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=active.id, class_name='Warrior', spec_name='Fury', rank=1,
            score=5000, region='eu', realm='Kazzak', character_name='Lightweight',
            gear_json=[{'payload': 'large'}], talents_json=[{'payload': 'large'}],
            stats_json={'payload': 'large'}, talent_build_code='LONG_BUILD',
        )

        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_fury')

        self.assertEqual(response.status_code, 200)
        player_queries = [query['sql'].lower() for query in queries.captured_queries
                          if 'wow_spec_top_player' in query['sql'].lower()]
        self.assertEqual(len(player_queries), 1)
        for large_field in ('gear_json', 'talents_json', 'stats_json', 'talent_build_code'):
            self.assertNotIn(large_field, player_queries[0])

    def test_top_players_excludes_cn_characters_from_battlenet_picker(self):
        active = SeasonMeta.objects.create(
            season_key='current-season-cn-filter', season_name='当前赛季', is_active=True,
            mplus_zone_id=2, raid_zone_id=2,
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=active.id, class_name='Warrior', spec_name='Fury', rank=1,
            score=6000, region='cn', realm='国服服务器', character_name='国服角色',
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=active.id, class_name='Warrior', spec_name='Fury', rank=2,
            score=5000, region='eu', realm='Kazzak', character_name='Availableplayer',
        )

        response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_fury')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([row['character'] for row in payload['data']], ['Availableplayer'])
        self.assertNotIn('cn', [row['region'] for row in payload['data']])

    def test_top_players_rejects_unknown_spec(self):
        response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_not_a_spec')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_preflight_rejects_cn_because_battlenet_cannot_load_cn_characters(self):
        from unittest.mock import patch

        with patch('botend.services.battlenet_preflight.fetch_battlenet_character_preflight') as fetch:
            response = self.client.post('/api/simc-battlenet-preflight/', data=json.dumps({
                'region': 'cn', 'realm': '国服服务器', 'character': '国服角色',
            }), content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('国服角色无法通过 Battle.net 加载', response.json()['error'])
        fetch.assert_not_called()

    def test_preflight_returns_fetched_character_and_simc_readiness(self):
        from unittest.mock import patch

        fetched = {
            'identity': {
                'name': 'Bloodmastêr', 'realm': 'Kazzak', 'region': 'eu',
                'class_name': 'warrior', 'level': 80,
            },
            'spec': {'key': 'fury', 'name': 'Fury'},
            'equipment': {'count': 15, 'item_level': 680},
            'stats': {'secondary': {'crit': {'rating': 1000}}},
            'simc_ready': True,
            'warnings': [],
        }
        with patch('botend.services.battlenet_preflight.fetch_battlenet_character_preflight', return_value=fetched) as fetch:
            response = self.client.post('/api/simc-battlenet-preflight/', data=json.dumps({
                'region': 'EU', 'realm': 'Kazzak', 'character': 'Bloodmastêr', 'spec': 'fury',
            }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertTrue(payload['data']['simc_ready'])
        self.assertEqual(payload['data']['identity']['name'], 'Bloodmastêr')
        self.assertEqual(payload['data']['spec']['key'], 'fury')
        fetch.assert_called_once_with(region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='fury')

    def test_preflight_service_parses_live_stats_and_rejects_missing_talent(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Bloodmastêr', 'level': 80,
            'character_class': {'name': 'Warrior'},
            'active_spec': {'name': 'Fury'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 680}}]}
        stats = {
            'strength': {'effective': 5000},
            'melee_crit': {'rating': 1000, 'value': 20.0},
            'melee_haste': {'rating': 2000, 'value': 15.0},
            'mastery': {'rating': 3000, 'value': 30.0},
            'versatility': {'rating': 4000, 'damageDoneBonus': 10.0},
        }
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, stats]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='fury',
            )

        self.assertTrue(result['simc_ready'], result)
        self.assertEqual(result['stats']['primary']['strength'], 5000)
        self.assertEqual(result['stats']['secondary']['crit']['rating'], 1000)
        self.assertEqual(result['simc_config']['gear_strength'], 5000)
        self.assertEqual(result['simc_config']['gear_versatility'], 4000)
        self.assertEqual(result['simc_config']['talent'], '')
        self.assertEqual(result['warnings'], [])

    def test_preflight_freezes_complete_battlenet_player_snapshot(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Snapshotter', 'level': 80, 'race': {'name': 'Orc'},
            'character_class': {'name': 'Warrior'}, 'active_spec': {'name': 'Fury'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [
            {
                'item': {'id': 212048}, 'name': 'Everforged Helm', 'slot': {'type': 'HEAD'},
                'level': {'value': 680}, 'bonus_list': [10255, 10390],
                'enchantments': [{'enchantment_id': 7352, 'display_string': 'Incandescent Essence'}],
                'sockets': [
                    {'item': {'id': 213743}, 'display_string': 'Culminating Blasphemite'},
                    {'item': {'id': 213744}, 'display_string': 'Masterful Ruby'},
                ],
            },
            {'item': {'id': 222222}, 'slot': {'type': 'MAIN_HAND'}, 'level': {'value': 680}},
        ]}
        stats = {'strength': {'effective': 5000}}
        specializations = {
            'active_specialization': {'id': 72},
            'specializations': [{
                'specialization': {'id': 72, 'name': 'Fury'},
                'loadouts': [
                    {
                        'is_active': True,
                        'talent_loadout_code': 'CwPAAAAAAAAAAAAAAAAAAAAAAMzMzMz',
                    },
                    {
                        'is_active': False,
                        'name': '团本屠戮',
                        'talent_loadout_code': 'CwPBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
                    },
                ],
            }],
        }
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get',
            side_effect=[profile, equipment, stats, specializations],
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Snapshotter', requested_spec='fury',
            )

        snapshot = result['simc_config']['player_equipment']
        self.assertIn('warrior="Snapshotter"', snapshot)
        self.assertIn('level=80', snapshot)
        self.assertIn('race=orc', snapshot)
        self.assertIn('spec=fury', snapshot)
        self.assertIn('head=,id=212048,bonus_id=10255/10390,enchant_id=7352,gem_id=213743/213744', snapshot)
        self.assertIn('main_hand=,id=222222', snapshot)
        self.assertEqual(result['simc_config']['talent'], 'CwPAAAAAAAAAAAAAAAAAAAAAAMzMzMz')
        self.assertEqual(result['talents'], {
            'build_code': 'CwPAAAAAAAAAAAAAAAAAAAAAAMzMzMz',
            'saved_loadouts': [{
                'name': '团本屠戮',
                'build_code': 'CwPBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
            }],
        })
        self.assertEqual(result['comparison_candidates'], {
            'default_talent': {
                'name': '默认天赋',
                'talent': 'CwPAAAAAAAAAAAAAAAAAAAAAAMzMzMz',
                'source': 'battlenet_active',
            },
            'talents': [{
                'name': '团本屠戮',
                'talent': 'CwPBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
                'source': 'battlenet_loadout',
            }],
            'gear': [],
        })
        self.assertNotIn('armory=', snapshot)
        self.assertEqual(result['equipment_summary'], {'count': 2, 'item_level': 680})
        self.assertEqual(result['equipment'][0], {
            'id': 212048,
            'display_name': 'Everforged Helm',
            'slot': 'head',
            'slot_label': '头盔',
            'item_level': 680,
            'enchant': {'id': 7352, 'display_name': 'Incandescent Essence'},
            'gems': [
                {'id': 213743, 'display_name': 'Culminating Blasphemite'},
                {'id': 213744, 'display_name': 'Masterful Ruby'},
            ],
            'bonus_ids': [10255, 10390],
        })
        self.assertEqual(result['equipment'][1]['slot'], 'main_hand')
        self.assertTrue(result['simc_ready'], result)

    def test_preflight_normalizes_spaced_battlenet_class_name(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Bloodmastêr', 'level': 90,
            'character_class': {'name': 'Death Knight'},
            'active_spec': {'name': 'Blood'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 292}}]}
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, {}]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='blood',
            )

        self.assertEqual(result['identity']['class_name'], 'deathknight')
        self.assertTrue(result['simc_ready'], result)
        self.assertEqual(result['warnings'], [])

    def test_preflight_rejects_unrecognized_active_spec_for_requested_target(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Unknownspec', 'level': 90,
            'character_class': {'name': 'Warrior'},
            'active_spec': {}, 'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 680}}]}
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, {}]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Unknownspec', requested_spec='fury',
            )

        self.assertFalse(result['simc_ready'])
        self.assertTrue(any('无法识别' in warning for warning in result['warnings']))


class SimcComparisonTaskAPIViewGetTests(TestCase):
    """Comparison detail is Task-scoped and aggregates only that Task's Runs."""

    def setUp(self):
        self.user = User.objects.create_user(username='comparison_get_user', password='pwd')
        self.other_user = User.objects.create_user(username='other_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def _task(self, user=None, mode='comparison', active=True, name='Comparison detail'):
        user = user or self.user
        return create_test_task(
            user_id=user.id, name=name, simc_profile_id=0, mode=mode,
            current_status=1, is_active=active,
        )

    def _run(self, task, sequence, status='pending', label='', params=None, summary=None):
        return SimulationRun.objects.create(
            task=task, sequence=sequence, candidate_key=f'candidate-{sequence}',
            candidate_label=label, candidate_params=params or {}, status=status,
            result_summary=summary,
        )

    def test_get_requires_task_id(self):
        response = self.client.get('/api/simc-task/comparison/')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_get_returns_task_and_run_status_counts(self):
        task = self._task(mode='attribute_sweep')
        self._run(task, 1, 'pending', '基准配置')
        self._run(task, 2, 'running', '候选 A')
        self._run(task, 3, 'completed', '候选 B', summary={'dps': 101000, 'secret': 'drop'})
        self._run(task, 4, 'failed', '候选 C')
        payload = self.client.get('/api/simc-task/comparison/', {'task_id': task.id}).json()
        self.assertTrue(payload['success'], payload)
        data = payload['data']
        self.assertEqual(data['task_id'], task.id)
        self.assertEqual(data['mode'], 'attribute_sweep')
        self.assertEqual(data['status_counts'], {
            'pending': 1, 'running': 1, 'completed': 1, 'failed': 1,
        })
        self.assertEqual([row['candidate_label'] for row in data['runs']],
                         ['基准配置', '候选 A', '候选 B', '候选 C'])
        self.assertEqual(data['runs'][2]['result_summary'], {'dps': 101000})
        self.assertEqual(data['runs'][3]['error_summary'], '任务执行失败')

    def test_get_isolated_by_task_relation(self):
        task = self._task(name='owned')
        other = self._task(name='also owned')
        self._run(task, 1, 'completed', 'expected', summary={'dps': 100})
        self._run(other, 1, 'completed', 'must not leak', summary={'dps': 999})
        runs = self.client.get('/api/simc-task/comparison/', {'task_id': task.id}).json()['data']['runs']
        self.assertEqual([row['candidate_label'] for row in runs], ['expected'])

    def test_get_enforces_user_isolation(self):
        foreign = self._task(user=self.other_user)
        self._run(foreign, 1)
        response = self.client.get('/api/simc-task/comparison/', {'task_id': foreign.id})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()['success'])

    def test_get_rejects_inactive_and_normal_tasks(self):
        inactive = self._task(active=False)
        normal = self._task(mode='normal')
        for task in (inactive, normal):
            self._run(task, 1)
            response = self.client.get('/api/simc-task/comparison/', {'task_id': task.id})
            self.assertEqual(response.status_code, 404)

    def test_get_does_not_expose_candidate_params_or_task_secrets(self):
        task = self._task()
        task.ext = json.dumps({'player_equipment': 'secret_config'})
        task.mode_params = {'request_manifest': {'secret': 'sensitive_data'}}
        task.save(update_fields=['ext', 'mode_params'])
        self._run(task, 1, 'completed', params={'talent_override': 'PRIVATE_BUILD'},
                  summary={'dps': 100, 'raw_output': 'PRIVATE_OUTPUT'})
        response_text = self.client.get(
            '/api/simc-task/comparison/', {'task_id': task.id},
        ).content.decode()
        for secret in ('secret_config', 'sensitive_data', 'PRIVATE_BUILD', 'PRIVATE_OUTPUT'):
            self.assertNotIn(secret, response_text)

    def test_regular_compare_uses_task_runs_and_preserves_named_talent_candidate(self):
        task = self._task()
        params = {
            'candidate_type': 'talent_override', 'is_base': False,
            'talent_override': 'MANUAL_TALENT_BUILD',
            'talent_candidate': {
                'name': '手工单体方案', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
            },
            'search': {'candidate_index': 0},
        }
        self._run(task, 1, 'completed', '手工单体方案', params, {'dps': 101000})
        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()
        self.assertTrue(payload['success'], payload)
        row = payload['data']['runs'][0]
        self.assertEqual(row['candidate'], {
            'type': 'talent', 'name': '手工单体方案',
            'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
        })

    def test_regular_compare_reports_progress_from_runs(self):
        task = self._task()
        self._run(task, 1, 'pending', 'baseline', {'is_base': True})
        self._run(task, 2, 'running', 'candidate')
        payload = self.client.get('/api/simc-regular-compare/', {'task_id': task.id}).json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['task']['pending'], 1)
        self.assertEqual(payload['data']['task']['running'], 1)

    def test_task_detail_report_url_uses_task_id(self):
        task = self._task()
        self._run(task, 1, 'completed', summary={'dps': 100})
        data = self.client.get('/api/simc-task/comparison/', {'task_id': task.id}).json()['data']
        self.assertEqual(data['report_url'], f'/simc-compare/?task_id={task.id}')

    def test_frontend_task_polling_contract(self):
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        start = workbench_js.index('async function loadTasks(')
        end = workbench_js.index('function renderPagination(', start)
        task_js = workbench_js[start:end]
        self.assertNotIn('setInterval(', task_js)
        self.assertIn('taskFetchInFlight', task_js)
        self.assertIn('taskRequestSerial', task_js)
        self.assertIn('setTimeout(', task_js)
        self.assertIn('scheduleTaskRefresh(hasActive)', task_js)
        self.assertIn('暂无记录', task_js)


class SimcResourceOwnershipBoundaryTests(TestCase):
    """Legacy and workbench endpoints must share the server-side ownership policy."""

    def setUp(self):
        self.user = User.objects.create_user(username='simc-boundary-user', password='pwd')
        self.other = User.objects.create_user(username='simc-boundary-other', password='pwd')
        self.admin = User.objects.create_superuser(username='simc-boundary-admin', password='pwd', email='admin@example.com')
        self.client = Client()
        self.own_profile = SimcProfile.objects.create(user_id=self.user.id, name='Own profile', spec='warrior_fury')
        self.other_profile = SimcProfile.objects.create(user_id=self.other.id, name='Other profile', spec='warrior_fury')
        self.system_profile = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:boundary', name='System profile', spec='warrior_fury',
        )
        self.global_nondefault_profile = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_USER, name='Global non-default', spec='warrior_fury',
        )
        self.own_apl = SimcApl.objects.create(
            owner_user_id=self.user.id, name='Own APL', spec='warrior_fury', content='actions=/auto_attack',
        )
        self.other_apl = SimcApl.objects.create(
            owner_user_id=self.other.id, name='Other APL', spec='warrior_fury', content='actions=/auto_attack',
            is_selectable=True,
        )

    def test_regular_user_cannot_list_read_edit_or_delete_other_resources(self):
        self.client.force_login(self.user)
        apls = self.client.get('/api/apl-storage/').json()['data']
        self.assertEqual({row['id'] for row in apls}, {self.own_apl.id})
        self.assertFalse(self.client.get(f'/api/apl-storage/{self.other_apl.id}/').json()['success'])
        self.assertFalse(self.client.put('/api/apl-storage/', data=json.dumps({
            'id': self.other_apl.id, 'title': 'forged', 'spec': 'warrior_fury', 'apl_code': 'actions=/auto_attack',
        }), content_type='application/json').json()['success'])
        self.assertFalse(self.client.delete('/api/apl-storage/', data=json.dumps({'id': self.other_apl.id}), content_type='application/json').json()['success'])
        self.assertFalse(self.client.get(f'/api/simc-profile/{self.other_profile.id}/').json()['success'])
        self.assertFalse(self.client.delete('/api/simc-profile/', data=json.dumps({'id': self.other_profile.id}), content_type='application/json').json()['success'])
        self.assertTrue(SimcApl.objects.filter(id=self.other_apl.id, is_active=True).exists())
        self.assertTrue(SimcProfile.objects.filter(id=self.other_profile.id).exists())

    def test_superuser_can_list_read_edit_and_delete_other_resources(self):
        self.client.force_login(self.admin)
        apls = self.client.get('/api/apl-storage/').json()['data']
        self.assertEqual({row['id'] for row in apls}, {self.own_apl.id, self.other_apl.id})
        self.assertTrue(self.client.get(f'/api/apl-storage/{self.other_apl.id}/').json()['success'])
        self.assertTrue(self.client.put('/api/apl-storage/', data=json.dumps({
            'id': self.other_apl.id, 'title': 'Edited by admin', 'spec': 'warrior_fury', 'apl_code': 'actions=/bloodthirst',
        }), content_type='application/json').json()['success'])
        self.assertTrue(self.client.delete('/api/apl-storage/', data=json.dumps({'id': self.other_apl.id}), content_type='application/json').json()['success'])
        self.assertTrue(self.client.get(f'/api/simc-profile/{self.other_profile.id}/').json()['success'])
        self.assertTrue(self.client.delete('/api/simc-profile/', data=json.dumps({'id': self.other_profile.id}), content_type='application/json').json()['success'])

    def test_superuser_copy_then_simulate_preserves_admin_resource_scope(self):
        self.other_profile.player_config_mode = 'manual_equipment'
        self.other_profile.player_equipment = 'warrior="Other"\nspec=fury\nhead=,id=1'
        self.other_profile.save(update_fields=['player_config_mode', 'player_equipment'])
        self.client.force_login(self.admin)
        with patch.object(SimcProfileAPIView, '_create_simulation_task', return_value={
            'success': True, 'data': {'id': 1, 'name': 'admin copy task', 'current_status': 0, 'mode': 'normal'},
        }) as create_task:
            response = self.client.post('/api/simc-profile/', data=json.dumps({
                'copy_from_id': self.other_profile.id, 'simulate_now': True,
            }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.content)
        self.assertTrue(create_task.call_args.kwargs['is_admin'])

    def test_simulation_selection_only_allows_own_or_explicit_system_defaults(self):
        from botend.services.simc_task_service import TaskCreationError, validate_resource_ownership

        validate_resource_ownership(self.own_profile, 'profile', self.user.id)
        validate_resource_ownership(self.system_profile, 'profile', self.user.id)
        with self.assertRaises(TaskCreationError):
            validate_resource_ownership(self.other_profile, 'profile', self.user.id)
        with self.assertRaises(TaskCreationError):
            validate_resource_ownership(self.global_nondefault_profile, 'profile', self.user.id)
        self.own_apl.is_system = True
        self.own_apl.save(update_fields=['is_system'])
        with self.assertRaises(TaskCreationError):
            validate_resource_ownership(self.own_apl, 'apl', self.other.id)
        validate_resource_ownership(self.other_apl, 'apl', self.admin.id, is_admin=True)

    def test_system_default_profile_is_loaded_without_mutation_for_task_creation(self):
        from botend.services.simc_task_service import create_task_from_request

        original_name = self.system_profile.name
        with patch('botend.services.simc_task_service.create_task', return_value=object()) as create_task, \
             patch.object(SimcProfile, 'save') as save_profile:
            result = create_task_from_request(
                user_id=self.user.id,
                profile_fields={'simc_profile_id': self.system_profile.id, 'name': 'must not overwrite'},
                base_template_id=999, selected_apl_id=998,
            )
        self.assertIs(result, create_task.return_value)
        save_profile.assert_not_called()
        self.system_profile.refresh_from_db()
        self.assertEqual(self.system_profile.name, original_name)
        self.assertEqual(create_task.call_args.kwargs['profile_id'], self.system_profile.id)

    def test_legacy_simulate_endpoint_accepts_system_default_but_not_arbitrary_global_profile(self):
        self.client.force_login(self.user)
        with patch.object(SimcProfileAPIView, '_create_simulation_task', return_value={
            'success': True, 'data': {'id': 1, 'name': 'system task', 'current_status': 0, 'mode': 'normal'},
        }) as create_task:
            response = self.client.post('/api/simc-profile/', data=json.dumps({
                'simulate_now': True, 'profile_id': self.system_profile.id,
            }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.content)
        self.assertEqual(create_task.call_args.args[1].id, self.system_profile.id)
        forbidden = self.client.post('/api/simc-profile/', data=json.dumps({
            'simulate_now': True, 'profile_id': self.global_nondefault_profile.id,
        }), content_type='application/json')
        self.assertFalse(forbidden.json()['success'])
