"""Cast completion must not retain streamed actor graphs between pairs."""
import copy
import gc
import weakref

from django.test import SimpleTestCase

from botend.services import simc_skill_damage as damage
from botend.tests.test_simc_partial_state_local_damage import REAL_ACTOR


class Actor(dict):
    pass


class CastLifecycleTests(SimpleTestCase):
    def test_streamed_actors_released_before_next_pair(self):
        refs = []

        class Variants:
            def __iter__(inner):
                for index in range(20):
                    gc.collect()
                    # The consumer's loop variable may still hold the last yield;
                    # no older pair may remain live, including in metadata passes.
                    self.assertLessEqual(sum(ref() is not None for ref in refs), 1)
                    selected = Actor(copy.deepcopy(REAL_ACTOR))
                    refs.append(weakref.ref(selected))
                    yield {
                        'talent': {'id': index, 'node_id': 30 + index, 'tree_type': 'spec'},
                        'reference_high': {'actions': []}, 'reference_low': {'actions': []},
                        'high': selected, 'low': selected,
                        'activation_context': {'trait_entry_ids': [10 + index], 'action_spell_ids': [335096]},
                    }
                    del selected

        rows = damage.flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, Variants(), global_effects=[],
        )
        self.assertEqual(len(rows), 60)
        gc.collect()
        self.assertFalse(any(ref() is not None for ref in refs))

    def test_duplicate_pair_preserves_first_source_and_global_grouping(self):
        def action(token, spell, hit):
            return {'token': token, 'spell_id': spell, 'supported': True,
                    'reporting_root_component': True, 'reporting_root_token': 'root',
                    'reporting_root_spell_id': 1, 'player_skill': True,
                    'baseline': {'direct': {'hit': hit}, 'unresolved_reason': None}, 'scenarios': []}

        root = action('root', 1, 10)
        child = action('child', 2, 20)
        other = action('other', 3, 30)
        first = {'actions': [root, child, other]}
        second = {'actions': [action('root', 1, 11), action('child', 2, 22)]}
        talent = {'id': 1, 'node_id': 1, 'tree_type': 'spec'}
        variants = []
        for selected, reference in ((first, {'actions': [child, other]}),
                                    (second, {'actions': [second['actions'][0]]})):
            variants.append({'talent': talent, 'high': selected, 'low': selected,
                             'reference_high': reference, 'reference_low': reference})
        rows = damage.flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, variants, global_effects=[],
        )
        self.assertEqual([(row['token'], row['baseline']['direct']['hit']) for row in rows],
                         [('root', 10), ('child', 22), ('other', 30)])
        self.assertTrue(rows[-1]['cast_component_unchanged'])
        self.assertNotIn('cast_component_unchanged', rows[1])
