"""Selection-only checks must not decode unrelated action/scenario graphs."""
import copy
from types import SimpleNamespace
from unittest import TestCase
from botend.services import simc_skill_activation_context as context


class IdentityFacts(dict):
    def get(self, key, default=None):
        if key != 'selected_trait_ids':
            raise AssertionError(f'Selection validation touched non-identity field {key}')
        return super().get(key, default)


class ActivationSelectionMetadataTests(TestCase):
    def setUp(self):
        self.pair = {
            'talent': SimpleNamespace(pk=30,node_id=3,tree_type='spec'),
            'reference_name':'before', 'selected_name':'after',
            'activation_context':{'action_spell_ids':[101],'trait_entry_ids':[2]},
            'expected_reference_trait_entry_ids':[1,2],
            'expected_selected_trait_entry_ids':[1,2,3],
        }
        self.facts = {
            (100,'before'):IdentityFacts(selected_trait_ids=[1,2]),
            (100,'after'):IdentityFacts(selected_trait_ids=[1,2,3]),
            (34,'before'):IdentityFacts(selected_trait_ids=[1,2]),
            (34,'after'):IdentityFacts(selected_trait_ids=[1,2,3]),
        }

    def test_identity_only_matches_materializer_without_touching_actions(self):
        before=copy.deepcopy(self.facts);calls=[]
        def load(health,name):
            calls.append((health,name));return self.facts.get((health,name))
        self.assertIsNone(context.validate_activation_context_selection(self.pair,load))
        self.assertEqual(calls,[(100,'before'),(100,'after'),(34,'before'),(34,'after')])
        self.assertEqual(self.facts,before)

    def test_all_existing_selection_failures_match_full_materializer(self):
        cases=[]
        f=copy.deepcopy(self.facts);f[(100,'before')]=None;cases.append((self.pair,f))
        f=copy.deepcopy(self.facts);f[(100,'after')]['selected_trait_ids']=[1,2,3,4];cases.append((self.pair,f))
        f=copy.deepcopy(self.facts);f[(100,'before')]['selected_trait_ids']=[1];f[(100,'after')]['selected_trait_ids']=[1,3];cases.append((self.pair,f))
        p=copy.deepcopy(self.pair);p['expected_selected_trait_entry_ids']=[1,2,3,4];cases.append((p,self.facts))
        p=copy.deepcopy(self.pair);p.pop('expected_reference_trait_entry_ids');p.pop('expected_selected_trait_entry_ids');f=copy.deepcopy(self.facts);f[(34,'before')]['selected_trait_ids']=[1,2,5];f[(34,'after')]['selected_trait_ids']=[1,2,3,5];cases.append((p,f))
        for pair,facts in cases:
            with self.subTest(facts=facts):
                def load(health,name):
                    row=facts.get((health,name))
                    return None if row is None else {**row,'actions':[{'spell_id':101}]}
                with self.assertRaises(ValueError) as full:
                    context.materialize_activation_pair(pair,load)
                with self.assertRaises(ValueError) as light:
                    context.validate_activation_context_selection(pair,lambda h,n:facts.get((h,n)))
                self.assertEqual(str(light.exception),str(full.exception))

    def test_full_materializer_keeps_roots_order_and_source_immutable(self):
        sources={key:{**value,'actions':[{'spell_id':99},{'spell_id':101,'token':'main'},{'spell_id':102,'reporting_root_spell_id':101,'token':'child'}]} for key,value in self.facts.items()}
        original=copy.deepcopy(sources)
        result=context.materialize_activation_pair(self.pair,lambda h,n:sources.get((h,n)))
        self.assertEqual([a['token'] for a in result['high']['actions']],['main','child'])
        self.assertEqual(result['high']['talent_effectiveness'],'active')
        self.assertEqual(result['reference_high']['talent_effectiveness'],'inactive')
        self.assertEqual(sources,original)
