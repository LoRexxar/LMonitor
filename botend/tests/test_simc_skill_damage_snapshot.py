import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings

from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, SimcBackendBinary,
    SimcSkillDamageSnapshot, WowTalentNodeMetadata, WowTalentVersion,
)
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
    def test_baselines_are_unique_per_spec_and_actual_hero_tree(self):
        profile = SimpleNamespace(pk=1, spec='warrior_fury', class_name='warrior')
        talents = [
            SimpleNamespace(pk=10, spec='warrior_fury', talent='SLAYER', modified_at=3, system_key='', hero_talent_names=[]),
            SimpleNamespace(pk=11, spec='warrior_fury', talent='MOUNTAIN_OLD', modified_at=1, system_key='', hero_talent_names=[]),
            SimpleNamespace(pk=12, spec='warrior_fury', talent='MOUNTAIN_NEW', modified_at=2, system_key='', hero_talent_names=[]),
        ]
        service = SimcSkillDamageSnapshotService(mock.Mock())
        resolved = {
            'SLAYER': ['屠戮者'],
            'MOUNTAIN_OLD': ['山丘领主'],
            'MOUNTAIN_NEW': ['山丘领主'],
        }
        with mock.patch.object(service, '_profiles', return_value=[profile]), \
             mock.patch.object(service, '_talents', return_value=talents), \
             mock.patch(
                 'botend.services.simc_skill_damage.resolve_hero_talent_names',
                 side_effect=lambda talent, _spec: resolved[talent],
             ):
            baselines = service._baselines()

        self.assertEqual(
            [(row[0].spec, row[2], row[1].talent) for row in baselines],
            [
                ('warrior_fury', '屠戮者', 'SLAYER'),
                ('warrior_fury', '山丘领主', 'MOUNTAIN_NEW'),
            ],
        )

    def test_existing_schema_two_snapshot_without_dbc_universe_can_refresh_in_place(self):
        backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'is_active': True,
                'current_version': 'f' * 40, 'latest_version': 'f' * 40,
                'game_build': '12.1.0.69300', 'simc_path': sys.executable,
            },
        )
        existing = SimcSkillDamageSnapshot.objects.create(
            simc_revision='f' * 40,
            game_build='12.1.0.69300',
            schema_revision=2,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'specialization': 'fury',
                'hero_talent_tree': '屠戮者',
                'actions': [],
            }]},
        )

        service = SimcSkillDamageSnapshotService.create_for_current_backend()

        self.assertEqual(service.snapshot.pk, existing.pk)
        self.assertEqual(service.snapshot.status, SimcSkillDamageSnapshot.STATUS_PENDING)
        self.assertEqual(service.backend.pk, backend.pk)

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
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=2,
        )
        profiles = [mock.Mock(pk=1, spec='fury'), mock.Mock(pk=2, spec='arcane')]
        outputs = [
            {'schema_version': 2, 'simc_revision': 'c' * 40, 'game_build': '12.1.0.69299',
             'normalization_basis': {'attack_power': 100.0, 'spell_power': 100.0,
                                     'crit_percent': 20.0, 'mastery_percent': 50.0},
             'actors': [{'spec': 'fury', 'actions': []}]},
            {'schema_version': 2, 'simc_revision': 'c' * 40, 'game_build': '12.1.0.69299',
             'normalization_basis': {'attack_power': 100.0, 'spell_power': 100.0,
                                     'crit_percent': 20.0, 'mastery_percent': 50.0},
             'actors': [{'spec': 'arcane', 'actions': []}]},
        ]
        talents = [SimpleNamespace(pk=11, name='Fury Slayer'), SimpleNamespace(pk=12, name='Arcane Spellslinger')]
        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_baselines', return_value=[
                 (profiles[0], talents[0], '屠戮者'),
                 (profiles[1], talents[1], '法术连击'),
             ]), mock.patch.object(service, '_run_profile_export', side_effect=outputs):
            result = service.generate()
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, snapshot.STATUS_SUCCEEDED)
        self.assertEqual([a['specialization'] for a in result['actors']], ['fury', 'arcane'])
        self.assertEqual([a['hero_talent_tree'] for a in result['actors']], ['屠戮者', '法术连击'])
        self.assertEqual([a['talent_name'] for a in result['actors']], ['Fury Slayer', 'Arcane Spellslinger'])
        self.assertEqual(result['identity'], {
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'schema_revision': 2,
        })
        self.assertNotIn('profile_id', result['identity'])
        self.assertNotIn('talent', result['identity'])

    def test_generate_skips_one_invalid_talent_baseline_without_failing_snapshot(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=2,
        )
        profiles = [SimpleNamespace(spec='paladin_retribution'), SimpleNamespace(spec='warrior_fury')]
        talents = [SimpleNamespace(name='Invalid', pk=1), SimpleNamespace(name='Valid', pk=2)]
        valid_output = {
            'actors': [{'class': 'warrior', 'specialization': 'fury', 'actions': []}],
            'unresolved': [],
        }
        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_baselines', return_value=[
            (profiles[0], talents[0], '圣殿骑士'),
            (profiles[1], talents[1], '屠戮者'),
        ]), mock.patch.object(
            service, '_run_profile_export', side_effect=[RuntimeError('choice node index invalid'), valid_output],
        ):
            result = service.generate()

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, snapshot.STATUS_SUCCEEDED)
        self.assertEqual(len(result['actors']), 1)
        self.assertEqual(result['actors'][0]['hero_talent_tree'], '屠戮者')
        self.assertEqual(result['unresolved'], [{
            'specialization': 'paladin_retribution',
            'hero_talent_tree': '圣殿骑士',
            'talent_id': 1,
            'reason': 'choice node index invalid',
        }])

    def test_generate_falls_back_within_same_hero_tree_after_stale_talent_fails(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=2,
        )
        profile = SimpleNamespace(spec='warrior_fury')
        stale = SimpleNamespace(name='Upstream Fury', pk=192)
        valid = SimpleNamespace(name='Fury Slayer', pk=95)
        valid_output = {
            'actors': [{'class': 'warrior', 'specialization': 'fury', 'actions': []}],
            'unresolved': [],
        }
        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(
            service, '_baselines', return_value=[(profile, stale, '屠戮者')],
        ), mock.patch.object(
            service, '_fallback_talents', return_value=[valid],
        ), mock.patch.object(
            service,
            '_run_profile_export',
            side_effect=[RuntimeError('selected node is not available'), valid_output],
        ) as run:
            result = service.generate()

        self.assertEqual(run.call_args_list, [mock.call(profile, stale), mock.call(profile, valid)])
        self.assertEqual(len(result['actors']), 1)
        self.assertEqual(result['actors'][0]['hero_talent_tree'], '屠戮者')
        self.assertEqual(result['actors'][0]['talent_name'], 'Fury Slayer')
        self.assertEqual(result['unresolved'], [])

    def test_schema_two_requires_exported_mathematical_expectation(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=2,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        payload = {
            'schema_version': 2,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{'actions': [{
                'supported': True,
                'baseline': {'direct': {
                    'hit': 424.2, 'crit': 848.4,
                    'crit_chance': 0.2, 'expected': 509.04,
                }, 'tick': None},
            }]}],
        }
        service._validate_export(payload)
        direct = payload['actors'][0]['actions'][0]['baseline']['direct']
        del direct['expected']
        with self.assertRaisesRegex(ValueError, '数学期望字段无效'):
            service._validate_export(payload)

        direct['expected'] = None
        direct['hit'] = None
        direct['crit'] = None
        payload['actors'][0]['actions'][0]['baseline']['unresolved_reason'] = 'runtime_non_finite_amount'
        service._validate_export(payload)

        payload['actors'][0]['actions'][0]['baseline'] = {
            'direct': None,
            'tick': None,
            'unresolved_reason': 'snapshot_child_signal_11',
        }
        service._validate_export(payload)

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
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=2,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'specialization': 'fury',
                'hero_talent_tree': '屠戮者',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [],
            }]},
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

    def test_get_localizes_skill_identity_and_left_cell_only_shows_name_and_spell_id(self):
        version = WowTalentVersion.objects.create(key='current', is_active=True)
        WowTalentNodeMetadata.objects.create(
            talent_version=version, class_name='Warrior', spec_name='Fury',
            node_id=1, spell_id=1001, display_spell_id=1001, name_zh='天赋中文技能',
        )
        symbol = SimcAplSymbol.objects.create(token='apl_action', symbol_kind='action')
        SimcAplSymbolScope.objects.create(
            symbol=symbol, class_name='warrior', spec='fury', spell_id=2002,
            name_zh='APL中文技能',
        )
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=2,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'class': 'warrior', 'specialization': 'fury',
                'hero_talent_tree': '屠戮者', 'talent_name': 'Fury Slayer',
                'actions': [
                    {'name': 'talent_action', 'token': 'talent_action', 'spell_id': 1001},
                    {'name': 'apl_action', 'token': 'apl_action', 'spell_id': 2002},
                ],
            }]},
        )
        request = self.factory.get('/api/simc-skill-damage/')
        request.user = self.user

        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        actions = json.loads(response.content)['data']['snapshot']['actors'][0]['actions']
        self.assertEqual(
            [(row['display_name'], row['spell_id']) for row in actions],
            [('天赋中文技能', 1001), ('APL中文技能', 2002)],
        )

        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        identity_renderer = script.split('function renderSimcSkillIdentity(action) {', 1)[1].split(
            'function renderSimcSkillDamageSnapshot(snapshot) {', 1,
        )[0]
        self.assertIn('action.display_name', identity_renderer)
        self.assertIn('action.spell_id', identity_renderer)
        self.assertNotIn('action.token', identity_renderer)
        self.assertNotIn('hero_talent_tree', identity_renderer)
        self.assertNotIn('talent_name', identity_renderer)
        damage_renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel() {', 1,
        )[0]
        self.assertNotIn('${skillMeta}<div', damage_renderer)

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
        self.assertIn('技能数学期望伤害对照', template)
        self.assertIn('默认读取最新 SimC；每次 DBC Build 更新后自动生成新快照', template)
        self.assertNotIn('AP/SP 归一化为 1', template)
        self.assertIn('simc-skill-damage-table', template)
        self.assertIn('data-dashboard-section="simc-skill-damage"', template)
        self.assertIn('id="simc-skill-damage"', template)
        self.assertIn("'skill-damage': 'simc-skill-damage'", script)
        self.assertIn('/api/simc-skill-damage/', script)
        self.assertIn('renderSimcSkillDamageSnapshot', script)
        self.assertIn('initSimcSkillDamagePanel();', script)
        self.assertNotIn('bg-gray-900 simc-skill-damage', template)

    def test_dashboard_shows_fixed_preset_and_exported_mathematical_expectation(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertIn('AP/SP 100.00', template)
        self.assertIn('暴击 20.00%', template)
        self.assertIn('精通 50.00%', template)
        self.assertIn('数学期望伤害', template)
        self.assertIn('formatSimcSkillDamageNumber', renderer)
        self.assertIn('hasFiniteSimcSkillDamageNumber', renderer)
        self.assertIn("typeof value === 'number' && Number.isFinite(value)", renderer)
        self.assertIn('amount.expected', renderer)
        self.assertIn("filter(item => item && typeof item === 'object')", renderer)
        for field in ('hit', 'crit', 'crit_chance', 'expected'):
            self.assertIn(field, renderer)
        self.assertNotIn('multiplier * 100', renderer)
        self.assertNotIn("direct ${hasDirectBaseline ? '100.00'", renderer)
        self.assertNotRegex(renderer, r'\.toFixed\((?!2\))')
        self.assertIn('html[data-dashboard-theme="dark"] #simc-skill-damage-panel', template)

    def test_dashboard_requires_spec_and_hero_tree_then_sorts_independent_expectation_column(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertIn('id="simc-skill-damage-hero-tree"', template)
        self.assertIn('请选择专精', template)
        self.assertIn('请选择英雄天赋树', template)
        self.assertIn('id="simc-skill-damage-sort-expected"', template)
        self.assertIn('归一化伤害期望', template)
        self.assertIn('技能暴击率', template)
        self.assertIn('selectedHeroTree', renderer)
        self.assertIn('sortDirection', renderer)
        self.assertIn('expectedSortValue', renderer)
        self.assertNotIn('全部专精', renderer)
        self.assertNotIn('scenario', renderer.lower())
        self.assertNotIn("component === 'direct' ? 'Direct' : 'Tick'", renderer)
        self.assertNotIn('font-bold uppercase text-stone-500', renderer)
        self.assertNotIn('职业 Buff', template)
