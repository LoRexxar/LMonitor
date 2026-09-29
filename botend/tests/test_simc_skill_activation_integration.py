import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
from contextlib import ExitStack

from botend.services import simc_skill_damage as damage
from botend.services.simc_skill_activation_context import plan_activation_context_pairs


def talent(entry):
    return SimpleNamespace(pk=entry, node_id=entry, talent_id=entry,
        tree_type='spec', max_points=1, db2_subtree_id=None,
        name=f'trait_{entry}', name_zh=f'天赋{entry}')


class ActivationIntegrationTests(TestCase):
    def test_invalid_native_pair_is_explicitly_unresolved_not_attributed(self):
        self.test_generator_exports_context_but_never_classifies_its_roots_as_global(corrupt=True)

    def test_generator_exports_context_but_never_classifies_its_roots_as_global(self, corrupt=False):
        unlock, target = talent(10), talent(30)
        for t in (unlock, target):
            t.description = t.description_zh = ''
        service = damage.SimcSkillDamageSnapshotService(SimpleNamespace(
            simc_revision='revision', game_build='build', schema_revision=45),
            backend=SimpleNamespace(pk=None))
        service._native_talent_catalog = [{
            'trait_entry_id': 10, 'spell_id': 200,
            'effects': [{'type': 6, 'subtype': 332, 'misc1': 100, 'value': 300}],
        }]
        profile = SimpleNamespace(spec='fury', class_name='warrior')
        exported = []

        def export(profile, talents, *, actor_plan, **kwargs):
            names = [a['name'] for a in actor_plan]
            exported.extend(names)
            return {'actors': [{
                'name': a['name'], 'class': 'warrior', 'specialization': 'fury',
                'selected_trait_ids': [t.node_id for t in a['selected_talents']] + (
                    [999] if corrupt and a['name'].startswith('skill_damage_context_') else []),
                'actions': [{'spell_id': 300}, {'spell_id': 999}],
            } for a in actor_plan]}

        def classify(high, low, variants):
            self.assertTrue(all('activation_context' not in v for v in variants))
            self.assertEqual(len(list(variants)), 2)
            return []

        def flatten(high, low, variants, **kwargs):
            first, second = list(variants), list(variants)
            self.assertEqual(first, second)
            contexts = [v for v in first if 'activation_context' in v]
            self.assertEqual(len(contexts), 0 if corrupt else 1)
            if contexts:
                self.assertEqual(contexts[0]['talent']['node_id'], 30)
                self.assertEqual(contexts[0]['high']['actions'], [{'spell_id': 300}])
            return []

        with ExitStack() as stack:
            for method, value in (('_talent_entries', [unlock, target]),
                                  ('_hero_talent_trees', []), ('_spec_root_scaffold', []),
                                  ('_implicit_prerequisite_nodes', []),
                                  ('_talent_prerequisite_map', {}),
                                  ('_global_damage_talent_catalog', {})):
                stack.enter_context(mock.patch.object(service, method, return_value=value))
            stack.enter_context(mock.patch.object(service, '_run_profile_export', side_effect=export))
            classifier = stack.enter_context(mock.patch.object(damage, 'classify_global_skill_effects', side_effect=classify))
            flattener = stack.enter_context(mock.patch.object(damage, 'flatten_single_talent_damage_variants', side_effect=flatten))
            stack.enter_context(mock.patch.object(damage, 'project_skill_damage_product_payload', side_effect=lambda p:p))
            stack.enter_context(mock.patch.object(damage, 'localize_skill_damage_payload', side_effect=lambda p:p))
            _, unresolved, _ = service._generate_profile_product_actor(profile)
            self.assertEqual(len(unresolved), 1 if corrupt else 0)
            if corrupt:
                self.assertEqual(unresolved[0]['reason'], 'activation_context_pair_unresolved')
                self.assertIn('999', unresolved[0]['diagnostic'])
        classifier.assert_called_once()
        flattener.assert_called_once()
        self.assertEqual(exported.count('skill_damage_context_0'), 2)
        self.assertEqual(exported.count('skill_damage_base'), 2)

    def test_isolated_child_receives_the_full_catalog(self):
        service = damage.SimcSkillDamageSnapshotService(SimpleNamespace(
            pk=1, simc_revision='revision', game_build='build'), backend=SimpleNamespace(pk=None))
        service._load_global_damage_talent_catalog({
            'schema_version': service.EXPORTER_SCHEMA_REVISION,
            'simc_revision': 'revision', 'game_build': 'build', 'talents': [],
            'talent_catalog': [{'trait_entry_id': 10, 'spell_id': 100, 'effects': []}],
        })
        actor = {'class': 'warrior', 'specialization': 'fury', 'actions': [],
                 'variant_model': 'single_talent_runtime',
                 'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions'}

        def child(command, **kwargs):
            payload = json.loads(Path(command[command.index('--scope-catalog') + 1]).read_text())
            self.assertEqual(payload['talent_catalog'], service._native_talent_catalog)
            self.assertEqual(payload['talents'], [])
            Path(command[command.index('--output') + 1]).write_text(json.dumps([actor, [], 0]))
            return SimpleNamespace(returncode=0, communicate=lambda **kw: ('', ''))

        with mock.patch.object(damage.subprocess, 'Popen', side_effect=child):
            result = service._generate_profile_product_actor_isolated(
                SimpleNamespace(pk=1535, spec='fury', class_name='warrior'))
        self.assertEqual(result, (actor, [], 0))

    def test_catalog_preserves_native_rows_without_polluting_global_dict(self):
        service = damage.SimcSkillDamageSnapshotService.__new__(damage.SimcSkillDamageSnapshotService)
        service.snapshot = SimpleNamespace(simc_revision='revision', game_build='build')
        native = [{'trait_entry_id': 10, 'spell_id': 100, 'effects': []}]
        payload = {'schema_version': service.EXPORTER_SCHEMA_REVISION,
                   'simc_revision': 'revision', 'game_build': 'build',
                   'talents': [], 'talent_catalog': native}
        self.assertEqual(service._load_global_damage_talent_catalog(payload), {})
        self.assertEqual(service._native_talent_catalog, native)

    def test_context_variants_reiterate_with_root_allowlist_and_named_traits(self):
        unlock, target = talent(10), talent(30)
        plan = plan_activation_context_pairs([unlock, target],
            candidates=[{'trait_entry_id': 10, 'replacement_spell_id': 300}],
            contexts={10: {'selected_talents': [unlock], 'actor': {
                'selected_trait_ids': [10], 'actions': [{'spell_id': 300}]}}})
        pair = plan['pairs'][0]
        self.assertEqual(pair['activation_context']['traits'][0]['name_zh'], '天赋10')
        with tempfile.TemporaryDirectory() as root:
            spool = damage._CanonicalActorSpool(root)
            for health in (100, 34):
                spool.store_many(health, [{'name': a['name'],
                    'selected_trait_ids': [t.node_id for t in a['selected_talents']],
                    'actions': [{'spell_id': 300}, {'spell_id': 999}]}
                    for a in plan['actors']])
            ordinary = [{'ordinary': True}]
            variants = damage._ActivationContextVariants(ordinary, plan['pairs'], spool)
            first, second = list(variants), list(variants)
            self.assertEqual(first, second)
            self.assertEqual(first[0], ordinary[0])
            self.assertEqual(first[1]['high']['actions'], [{'spell_id': 300}])
            self.assertEqual(first[1]['activation_context']['traits'][0]['trait_entry_id'], 10)
