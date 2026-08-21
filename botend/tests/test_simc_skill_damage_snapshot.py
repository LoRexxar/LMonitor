import json
import sys
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings

from botend.models import SimcBackendBinary, SimcSkillDamageSnapshot
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService
from botend.dashboard.api import SimcSkillDamageSnapshotAPIView


class SimcSkillDamageSnapshotModelTests(TestCase):
    def test_identity_is_only_revision_game_build_and_schema_revision(self):
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='a' * 40, game_build='12.1.0.69299', schema_revision=1,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcSkillDamageSnapshot.objects.create(
                    simc_revision='a' * 40, game_build='12.1.0.69299', schema_revision=1,
                )

    def test_latest_success_ignores_newer_failed_snapshot(self):
        succeeded = SimcSkillDamageSnapshot.objects.create(
            simc_revision='a' * 40, game_build='12.1.0.69299', schema_revision=1,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{'specialization': 'fury'}]},
        )
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='b' * 40, game_build='12.1.0.69300', schema_revision=1,
            status=SimcSkillDamageSnapshot.STATUS_FAILED, error_text='broken',
        )
        self.assertEqual(SimcSkillDamageSnapshot.latest_success().pk, succeeded.pk)


class SimcSkillDamageSnapshotServiceTests(TestCase):
    @override_settings(SIMC_CONFIG={'simc_path': sys.executable})
    def test_configured_runtime_binary_overrides_stale_backend_path(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=1,
        )
        service = SimcSkillDamageSnapshotService(
            snapshot,
            backend=mock.Mock(simc_path='/stale/machine/simc'),
        )
        self.assertEqual(service._binary_path(), sys.executable)

    def test_generate_merges_actor_outputs_and_preserves_dataset_identity(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=1,
        )
        profiles = [mock.Mock(pk=1, spec='fury'), mock.Mock(pk=2, spec='arcane')]
        outputs = [
            {'schema_version': 1, 'simc_revision': 'c' * 40, 'game_build': '12.1.0.69299',
             'normalization_basis': {'attack_power': 1.0, 'spell_power': 1.0},
             'actors': [{'spec': 'fury', 'actions': []}]},
            {'schema_version': 1, 'simc_revision': 'c' * 40, 'game_build': '12.1.0.69299',
             'normalization_basis': {'attack_power': 1.0, 'spell_power': 1.0},
             'actors': [{'spec': 'arcane', 'actions': []}]},
        ]
        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_profiles', return_value=profiles), \
             mock.patch.object(service, '_run_profile_export', side_effect=outputs):
            result = service.generate()
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, snapshot.STATUS_SUCCEEDED)
        self.assertEqual([a['specialization'] for a in result['actors']], ['fury', 'arcane'])
        self.assertEqual(result['identity'], {
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'schema_revision': 1,
        })
        self.assertNotIn('profile_id', result['identity'])
        self.assertNotIn('talent', result['identity'])

    def test_dbc_refresh_uses_latest_backend_revision_and_only_runs_for_new_build(self):
        backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'is_active': True,
                'current_version': 'e' * 40, 'latest_version': 'e' * 40,
                'game_build': '12.1.0.69300', 'simc_path': sys.executable,
            },
        )
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='d' * 40, game_build='12.1.0.69300', schema_revision=1,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
        )

        with mock.patch.object(SimcSkillDamageSnapshotService, 'generate') as generate:
            self.assertIsNone(SimcSkillDamageSnapshotService.refresh_after_dbc_update())
            generate.assert_not_called()

            backend.game_build = '12.1.0.69301'
            backend.save(update_fields=['game_build'])
            snapshot = SimcSkillDamageSnapshotService.refresh_after_dbc_update()

        self.assertEqual(snapshot.simc_revision, 'e' * 40)
        self.assertEqual(snapshot.game_build, '12.1.0.69301')
        generate.assert_called_once_with()


class SimcSkillDamageSnapshotAPITests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username='viewer', password='x')
        self.staff = get_user_model().objects.create_user(username='staff', password='x', is_staff=True)

    def test_get_returns_latest_success_without_profile_filters(self):
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='d' * 40, game_build='12.1.0.69299', schema_revision=1,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{'specialization': 'fury'}]},
        )
        request = self.factory.get('/api/simc-skill-damage/', {'profile_id': 99, 'talent': 'x'})
        request.user = self.user
        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['data']['snapshot']['identity']['game_build'], '12.1.0.69299')
        self.assertNotIn('profile_id', body['data'])
        self.assertFalse(body['data']['can_generate'])

    def test_post_requires_staff(self):
        request = self.factory.post('/api/simc-skill-damage/', data='{}', content_type='application/json')
        request.user = self.user
        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        self.assertEqual(response.status_code, 403)


class SimcSkillDamageDashboardContractTests(TestCase):
    def test_dashboard_has_independent_light_skill_damage_panel(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        self.assertIn('id="simc-skill-damage-panel"', template)
        self.assertIn('技能基础伤害对照', template)
        self.assertIn('默认读取最新 SimC；每次 DBC Build 更新后自动生成新快照', template)
        self.assertIn('AP/SP 归一化为 1', template)
        self.assertIn('simc-skill-damage-table', template)
        self.assertIn('data-dashboard-section="simc-skill-damage"', template)
        self.assertIn('id="simc-skill-damage"', template)
        self.assertIn("'skill-damage': 'simc-skill-damage'", script)
        self.assertIn('/api/simc-skill-damage/', script)
        self.assertIn('renderSimcSkillDamageSnapshot', script)
        self.assertIn('initSimcSkillDamagePanel();', script)
        self.assertNotIn('bg-gray-900 simc-skill-damage', template)

    def test_dashboard_normalizes_baseline_to_100_and_shows_two_decimal_attribute_preset(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertIn('属性预制值', template)
        self.assertIn('基础伤害归一值（100.00）', template)
        self.assertIn('formatSimcSkillDamageNumber', renderer)
        self.assertIn('hasFiniteSimcSkillDamageNumber', renderer)
        self.assertIn("typeof value === 'number' && Number.isFinite(value)", renderer)
        self.assertIn('multiplier * 100', renderer)
        self.assertIn("filter(item => item && typeof item === 'object')", renderer)
        for field in ('primary_attribute', 'attack_power', 'spell_power', 'crit', 'haste', 'mastery', 'versatility'):
            self.assertIn(field, renderer)
        self.assertNotRegex(renderer, r'\.toFixed\((?!2\))')
        self.assertIn('html[data-dashboard-theme="dark"] #simc-skill-damage-panel', template)
