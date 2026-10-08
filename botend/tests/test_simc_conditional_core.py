"""Conditional contracts from reviewed same-build facts; no item special cases."""
import copy
import json
import unittest
from pathlib import Path
import simc_equipment_control as core
from simc_equipment_conditional import validate_contract, passive_effects, validate_pair_witness

AUDIT = Path('/tmp/lmonitor-embellishment-audit/conditional-exact6c50')


def fixture():
    data=json.loads(Path(__file__).with_name('fixtures').joinpath('conditional_samebuild_contract.json').read_text())
    return data['policy'],data['expectation'],data['authorization']


class ConditionalCoreTests(unittest.TestCase):
    def test_explicit_roles_mark_and_trust(self):
        policy, expectation, auth = fixture()
        code='\n'.join(t['slot']+'=,id='+str(t['item_id']) for t in expectation['targets'])
        marked = core.mark_equipment_input(code, policy['target_slots'], control=True,
            rules=policy['rules'], expectation=expectation, policy=policy)
        self.assertIn(core.MARKER, marked)
        validate_contract(policy, expectation, authorization=auth)
        for mutate in (lambda p,e: p.update(context_slots=p['changed_slots']),
                       lambda p,e: e['relations'].clear(),
                       lambda p,e: e['relations'][0].update(factor=3),
                       lambda p,e: e['identity'].update(dbc_build='12.1.0.1'),
                       lambda p,e: e['targets'][0].update(role='changed'),
                       lambda p,e: e['relations'][0]['source_fact'].update(source_clean_before=False)):
            p,e = copy.deepcopy((policy,expectation)); mutate(p,e)
            with self.assertRaises(ValueError): validate_contract(p,e,authorization=auth)
        with self.assertRaises(ValueError): validate_contract(policy,expectation,authorization=None)

    def test_unknown_is_not_globally_authorized(self):
        policy,expectation,auth=fixture()
        relation=validate_contract(policy,expectation,authorization=auth)
        target=next(t for t in expectation['targets'] if t['role']=='changed')
        export={'profile_value':',id='+str(target['item_id']), 'record':
            'effect={ type=unknown source=item driver='+str(relation['modifier_driver_spell_id'])+' }','effects':[]}
        self.assertEqual(passive_effects(export,target,relation)[0]['type'],'unknown')
        self.assertFalse(core._item_effects(export))
        for record in ('effect={ type=unknown source=item driver=1 }',
                       'effect={ type=unknown source=gem driver='+str(relation['modifier_driver_spell_id'])+' }',
                       export['record']+' '+export['record']):
            with self.assertRaises(ValueError): passive_effects({**export,'record':record},target,relation)
        # A known disabled/no-proc effect cannot inherit this unrelated relation.
        coiled=policy['rules']['embellishments'].get('coiled_jewel')
        if coiled:
            changed={**export,'record':'effect={ type=unknown source=item driver='+str(coiled['spell_id'])+' }'}
            with self.assertRaises(ValueError): passive_effects(changed,target,relation)

    def test_pair_requires_both_sides(self):
        self.assertIsNone(validate_pair_witness(None,None)['valid'])
        self.assertEqual(validate_pair_witness(None,{'status':'valid'})['status'],'pair_pending')
        self.assertFalse(validate_pair_witness({'status':'valid'},{'status':'valid'})['valid'])

if __name__ == '__main__': unittest.main()
