"""归一化伤害生成 APL 的分档、条件合并和入库回归。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from botend.models import SimcApl, SimcSkillDamageSnapshot
from botend.services.simc_apl.from_skill_damage import TARGETS, build_apl, native_talent_guards
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService as Snapshots
from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage as Storage


TREES = [{'id': 60}, {'id': 61}]
DEFAULT = 'actions.precombat=snapshot_stats\nactions=auto_attack\nactions+=/execute\nactions+=/whirlwind\n'


def sample(token, damage, *, variant=None, **fields):
    return {'token': token, 'variant': variant or {}, **fields, 'product': {
        'final_normalized_damage_by_target': dict(zip(map(str, TARGETS), damage)),
    }}


class SkillAplRankingTests(SimpleTestCase):
    def test_unlabelled_prerequisite_requires_exact_catalog_mapping(self):
        prerequisite = {'reference_context': {'trait_entry_ids': [812], 'traits': []}}
        rows = [sample('whirlwind', [1]*5), sample('execute', [2]*5),
                sample('whirlwind', [20]*5, variant=prerequisite)]
        without = build_apl(DEFAULT, rows, TREES, 'warrior_arms')
        with_catalog = build_apl(DEFAULT, rows, TREES, 'warrior_arms', talent_guards={812: 'talent.test_2'})
        self.assertEqual(without, DEFAULT)
        self.assertIn('talent.test_2', with_catalog)
        self.assertIn('actions+=/execute', with_catalog)

    def test_disintegrate_keeps_distinct_channel_options_and_intervening_control(self):
        channel = 'disintegrate,target_if=max:dot.fire_breath_damage.remains,chain=1,early_chain_if=ticks_remain<=1,interrupt_if=ticks_remain<=1'
        variant = 'disintegrate,early_chain_if=ticks_remain<=1,interrupt_if=active_enemies>=3'
        template = (f'actions=azure_strike\nactions+=/{channel},if=buff.hover.up\n'
                    f'actions+=/pool_resource,for_next=1\nactions+=/{variant}\n')
        result = build_apl(template, [sample('azure_strike', [2]*5), sample('disintegrate', [5]*5)],
                           [{'id': 36}, {'id': 37}], 'evoker_devastation')
        self.assertNotEqual(result, template)
        self.assertIn(channel, result)
        self.assertTrue(result.endswith(f'actions+=/pool_resource,for_next=1\nactions+=/{variant}\n'))
        for line in result.splitlines():
            if 'disintegrate' in line and variant not in line:
                self.assertIn(channel, line)
                self.assertIn('buff.hover.up', line)

    def test_native_hero_talent_name_collision_uses_original_dbc_index(self):
        common = {'class_id': 12, 'tree_index': 3, 'name': 'Flamebound'}
        rows = [{**common, 'trait_entry_id': 23, 'spec_ids': [577]},
                {**common, 'trait_entry_id': 11, 'spec_ids': [1480]},
                {**common, 'tree_index': 4, 'trait_entry_id': 77, 'spec_ids': [577], 'subtree_id': 34}]
        self.assertEqual(native_talent_guards(rows, 12, 577), {23: 'talent.flamebound_1', 77: 'hero_tree.felscarred'})
        self.assertEqual(native_talent_guards(rows, 12, 1480), {11: 'talent.flamebound_2'})

    def test_hero_target_and_stack_conditions_merge_without_moving_across_other_skills(self):
        rows = [sample('execute', [50] * 5), sample('whirlwind', [10] * 5)]
        for level in (2, 3, 4):
            rows.append(sample('whirlwind', [200] * 5, hero_subtree_ids=[60],
                               affected_target_counts=[2, 5], variant={
                                   'trait_entry_id': 100, 'talent_name': '同名天赋',
                                   'activation_context': {'trait_entry_ids': [200], 'traits': [
                                       {'trait_entry_id': 200, 'name': '前置天赋'}]},
                                   'runtime_condition': '血量低于35%，且自身存在增益时',
                                   'scenario_tokens': ['buff.whirlwind'],
                                   'runtime_conditions': [{'token': 'buff.whirlwind', 'stacks': level}]}))
        result = build_apl(DEFAULT, rows, TREES, 'warrior_arms',
                           talent_guards={100: 'talent.special_2', 200: 'talent.required'})
        boosted = [line for line in result.splitlines() if 'stack' in line]
        self.assertEqual(len(boosted), 1)
        for condition in ('hero_tree.slayer', 'active_enemies>=2', 'active_enemies<10',
                          'buff.whirlwind.stack>=2', 'buff.whirlwind.stack<=4',
                          'talent.special_2', 'talent.required', 'target.health.pct<35'):
            self.assertIn(condition, boosted[0])
        self.assertNotIn('hero_tree.mountain_thane', boosted[0])
        self.assertEqual(result.splitlines()[0], DEFAULT.splitlines()[0])

    def test_augmentation_compares_filler_at_essence_cap_after_support_actions(self):
        template = ('actions=eruption,if=buff.ebon_might_self.up\nactions+=/call_action_list,name=filler\n'
                    'actions.filler=living_flame,if=buff.leaping_flames.up\nactions.filler+=/azure_strike\n')
        result = build_apl(template, [sample('eruption', [5]*5), sample('azure_strike', [2]*5)],
                           [{'id': 36}, {'id': 38}], 'evoker_augmentation')
        self.assertTrue(result.startswith(template.partition('actions.filler+=')[0]))
        self.assertIn('essence.deficit=0', result)
        self.assertLess(result.index('living_flame'), result.index('essence.deficit=0'))

    def test_distinct_target_selectors_and_utility_boundary_survive(self):
        template = ('actions=execute,target_if=min:target.health.pct\n'
                    'actions+=/whirlwind\nactions+=/use_items\n'
                    'actions+=/execute,target_if=max:target.health.pct\n')
        result = build_apl(template, [sample('execute', [1]*5), sample('whirlwind', [2]*5)],
                           TREES, 'warrior_arms')
        self.assertIn('execute,target_if=min:target.health.pct', result)
        self.assertTrue(result.endswith('actions+=/use_items\nactions+=/execute,target_if=max:target.health.pct\n'))

    def test_unresolved_talent_or_unknown_runtime_condition_cannot_promote_action(self):
        rows = [sample('execute', [2]*5), sample('whirlwind', [1]*5)]
        for variant in ({'trait_entry_id': 124009, 'talent_name': 'Flamebound'},
                        {'runtime_condition': '未知条件'}, {'scenario_tokens': ['buff.unknown']}):
            rows.append(sample('whirlwind', [200]*5, variant=variant))
        result = build_apl(DEFAULT, rows, TREES, 'warrior_arms')
        self.assertNotIn('flamebound', result)
        self.assertLess(result.index('/execute'), result.index('/whirlwind'))

    def test_current_catalog_cannot_reassign_a_named_historical_prerequisite(self):
        rows = [sample('execute', [2]*5), sample('whirlwind', [1]*5),
                sample('whirlwind', [20]*5, variant={'trait_entry_id': 123, 'talent_name': 'Old Talent'})]
        for guard, expected in [('talent.old_talent_2', True), ('talent.replacement', False)]:
            result = build_apl(DEFAULT, rows, TREES, 'warrior_arms',
                               talent_guards={123: guard}, check_trait_names=True)
            self.assertEqual(guard in result, expected)
            if not expected:
                self.assertEqual(result, DEFAULT)


class SkillAplStorageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='技能页用户')
        self.client.force_login(self.user)
        self.url = reverse('simc_skill_damage_apl')
        self.snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='a'*40, game_build='12.1', schema_revision=Snapshots.DATASET_SCHEMA_REVISION,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED, generated_spec_count=1,
            payload={'payload_format': Snapshots.PAYLOAD_FORMAT, 'storage_format': Snapshots.STORAGE_FORMAT,
                     'wire_schema_revision': Snapshots.WIRE_SCHEMA_REVISION, 'total_spec_count': 1})
        small_parts = patch.object(Storage, 'PART_CHARACTERS', 200)
        small_parts.start()
        self.addCleanup(small_parts.stop)
        self.actor = Storage.create_actor(snapshot=self.snapshot, ordinal=0, class_name='warrior',
            specialization='arms', actor_payload={'hero_talent_trees': TREES, 'actions': [
                sample('execute', [1]*5), sample('whirlwind', [2]*5)]})
        self.template = SimcApl.objects.create(name='默认', class_name='warrior', spec='warrior_arms',
            source=SimcApl.SOURCE_SIMC_UPSTREAM, is_system=True, content=DEFAULT,
            sync_version=self.snapshot.simc_revision)

    def generate(self, **kwargs):
        return self.client.post(self.url,
            {'snapshot_id': self.snapshot.pk, 'actor_id': self.actor.pk, **kwargs},
            content_type='application/json')

    def old_actor_with_prerequisite(self):
        self.actor.delete()
        self.actor = Storage.create_actor(snapshot=self.snapshot, ordinal=0, class_name='warrior',
            specialization='arms', actor_payload={'hero_talent_trees': TREES, 'actions': [
                sample('execute', [2]*5), sample('whirlwind', [8]*5, variant={
                    'reference_context': {'trait_entry_ids': [101], 'traits': []}})]})

    def test_old_snapshot_reuses_current_native_catalog_without_version_arguments(self):
        from botend.models import SimcBackendBinary
        from botend.services.simc_skill_damage import _read_apl_guards

        SimcBackendBinary.objects.update_or_create(identifier='production', defaults={
            'current_version': 'b'*40, 'game_build': '12.2',
            'simc_path': __file__, 'is_active': True,
        })
        self.old_actor_with_prerequisite()

        def read_native(command, **kwargs):
            binary, output = command
            self.assertEqual(binary, __file__)
            option, path = output.split('=', 1)
            self.assertEqual(option, 'skill_damage_raw_scope_export')
            Path(path).write_text(json.dumps({'talent_catalog': [
                {'class_id': 1, 'tree_index': 2, 'spec_ids': [71],
                 'name': 'Native Prerequisite', 'trait_entry_id': 101}]}), encoding='utf-8')

        _read_apl_guards.cache_clear()
        self.addCleanup(_read_apl_guards.cache_clear)
        with patch('botend.services.simc_skill_damage.subprocess.run', side_effect=read_native) as export:
            for title, version in [('另一版本模板', 'c'*40), ('无版本模板', '')]:
                SimcApl.objects.filter(pk=self.template.pk).update(sync_version=version)
                response = self.generate(title=title)
                self.assertEqual(response.status_code, 201, response.content)
                saved = SimcApl.objects.get(pk=response.json()['data']['id'])
                self.assertEqual(saved.sync_version, version)
                self.assertIn('talent.native_prerequisite', saved.content)
            self.assertEqual(export.call_count, 1)

    def test_old_snapshot_uses_scoped_talents_without_installed_binary(self):
        from botend.models import SimcAplSymbol, SimcAplSymbolScope, SimcBackendBinary

        SimcBackendBinary.objects.filter(identifier='production').update(is_active=False)
        self.old_actor_with_prerequisite()
        self.assertEqual(self.generate().status_code, 422)
        for token in ('first_prerequisite', 'ambiguous_prerequisite'):
            symbol = SimcAplSymbol.objects.create(token=token, symbol_kind=SimcAplSymbol.KIND_TALENT)
            SimcAplSymbolScope.objects.create(symbol=symbol, class_name='warrior', spec='arms', trait_id=101)
            response = self.generate(title=token)
            if token == 'first_prerequisite':
                self.assertEqual(response.status_code, 201, response.content)
                self.assertIn('talent.first_prerequisite', SimcApl.objects.get(pk=response.json()['data']['id']).content)
            else:
                self.assertEqual(response.status_code, 422, response.content)

    def test_augmentation_probe_only_supplements_new_datasets_and_never_creates_a_profile(self):
        from botend.models import SimcProfile

        service = Snapshots(self.snapshot, backend=SimpleNamespace())
        with patch('botend.services.simc_skill_damage.SIMC_REQUIRED_PROFILE_SPECS', set()):
            service.snapshot.schema_revision = 60
            probe, = service._profiles()
            self.assertIsNone(probe.pk)
            self.assertEqual(probe.spec, 'evoker_augmentation')
            self.assertFalse(SimcProfile.objects.exists())
            saved = SimcProfile.objects.create(class_name='evoker', spec='evoker_augmentation',
                source=SimcProfile.SOURCE_SIMC_UPSTREAM, name='上游角色')
            self.assertEqual(service._profiles(), [saved])

    def test_legacy_snapshot_preserves_profile_membership(self):
        from botend.constants.simc_specs import SIMC_REQUIRED_PROFILE_SPECS
        from botend.models import SimcProfile

        profiles = [SimpleNamespace(class_name=c, spec=s) for c, s in SIMC_REQUIRED_PROFILE_SPECS]
        with patch.object(SimcProfile.objects, 'filter') as query:
            query.return_value.order_by.return_value = profiles
            self.snapshot.schema_revision = 59
            self.assertEqual(Snapshots(self.snapshot, backend=SimpleNamespace())._profiles(), profiles)

    def test_personal_draft_from_shards_and_duplicate_does_not_overwrite(self):
        self.assertTrue(self.actor.payload_storage)
        response = self.generate()
        self.assertEqual(response.status_code, 201, response.content)
        saved = SimcApl.objects.get(pk=response.json()['data']['id'])
        self.assertEqual((saved.name, saved.owner_user_id, saved.spec),
                         ('wowdaily自动生成', self.user.pk, 'warrior_arms'))
        self.assertIn('whirlwind', saved.content)
        listed = self.client.get(reverse('apl_storage')).json()['data']
        self.assertIn({'id': saved.pk, 'title': saved.name, 'spec': saved.spec}, listed)
        self.assertEqual(saved.validation_status, SimcApl.VALIDATION_DRAFT)
        self.assertFalse(saved.is_system or saved.is_selectable)
        self.assertEqual(self.generate().status_code, 409)
        self.assertEqual(self.generate(title='另一份草稿').status_code, 201)
        self.template.refresh_from_db()
        self.assertEqual(self.template.content, DEFAULT)

    def test_wrong_snapshot_actor_or_missing_template_creates_no_record(self):
        self.assertEqual(self.generate(snapshot_id=self.snapshot.pk+1).status_code, 409)
        self.assertEqual(self.generate(actor_id=self.actor.pk+1).status_code, 404)
        self.assertEqual(self.generate(title='').status_code, 400)
        SimcApl.objects.filter(pk=self.template.pk).update(is_active=False, active_unique_key=None)
        self.assertEqual(self.generate().status_code, 409)
        self.assertFalse(SimcApl.objects.filter(owner_user_id=self.user.pk).exists())
        self.client.logout()
        self.assertEqual(self.generate().status_code, 302)
