"""验证证据门槛与独立审计能发现漏声明，防止把未知默认为局部。"""
import copy
import importlib.util
from pathlib import Path
import unittest

module_path=Path(__file__).resolve().parents[2]/'scripts/simc_scope_evidence.py'
spec=importlib.util.spec_from_file_location('scope_evidence',module_path)
evidence=importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def row(description, **changes):
    return {'spell_id':10,'name':'测试天赋','description':description,'tooltip':'',
            'global_effect_indices':[], 'effects':[{'index':1,'id':101,'type':6,'subtype':108,
                'misc1':0,'misc2':0,'value':20,'flags':[1,0,0,0],'trigger':0}],**changes}


class ScopeEvidenceTests(unittest.TestCase):
    def test_unknown_and_global_subject_cannot_be_certified_local(self):
        names={1:'Fireball',2:'Frostbolt',3:'Fire',4:'Auto Shot'}
        for text in ('Your Fire damage is increased by $s1%.',
                     'Auto Shot damage increased by $s1%.',
                     'Fireball causes your damage to increase by $s1%.',
                     'Your attacks deal $s1% additional damage.',
                     'An unknown mechanism modifies damage by $s1%.'):
            with self.subTest(text=text):
                self.assertEqual(evidence.local_scope_evidence(row(text),names),{})

    def test_named_skill_set_has_positive_evidence(self):
        r=row('Fireball and Frostbolt damage increased by $s1%.')
        found=evidence.local_scope_evidence(r,{1:'Fireball',2:'Frostbolt'})
        self.assertEqual(set(found[1]['具名技能']),{'Fireball','Frostbolt'})

    def test_conditions_are_separate_and_bounded(self):
        self.assertEqual(evidence.text_variants('Your $?c1[Fire]?c2[Frost][Shadow] spells deal $?c1[$s1][$s2]% more damage.'),
            sorted(f'Your {school} spells deal {value}% more damage.' for school in ('Fire','Frost','Shadow') for value in ('$s1','$s2')))
        self.assertEqual(evidence.text_variants('$?c1[broken'),[])
        self.assertEqual(evidence.text_variants('$?c1[A][B]$?c2[C][D]',limit=2),[])

    def test_equal_numbers_do_not_prove_shared_local_scope(self):
        r=row('Fireball damage increased by $s1%. Another effect alters damage.')
        r['effects'].append({**r['effects'][0],'index':2,'id':102,'flags':[2,0,0,0]})
        self.assertEqual(set(evidence.local_scope_evidence(r,{1:'Fireball'})),{1})

    def test_independent_oracle_detects_absent_global_declaration(self):
        r=row('Your damage increased by $s1%.')
        fixture={'cases':[{**copy.deepcopy(r),'expected':[1]}]}
        self.assertIn('漏提取 [1]',evidence.verify_independent_expectations({'talent_catalog':[r]},fixture)[0])
        r['global_effect_indices']=[1]
        self.assertEqual(evidence.verify_independent_expectations({'talent_catalog':[r]},fixture),[])

    def test_independent_oracle_detects_local_overclassification_and_drift(self):
        r=row('Fireball damage increased by $s1%.')
        fixture={'cases':[{**copy.deepcopy(r),'expected':[]}]}
        r['global_effect_indices']=[1]
        self.assertIn('误提取 [1]',evidence.verify_independent_expectations({'talent_catalog':[r]},fixture)[0])
        r['effects'][0]['value']=30
        self.assertIn('源数据发生变化',evidence.verify_independent_expectations({'talent_catalog':[r]},fixture)[0])

    def test_no_damage_declaration_does_not_create_candidate(self):
        for text in ('Healing increased by $s1%.', 'Your shield absorbs $s1% more damage.',
                     'Damage taken is reduced by $s1%.', 'Heal for $s1% of the damage dealt.'):
            self.assertEqual(evidence.damage_parts(row(text)),[])

    def test_scripted_defense_and_resource_do_not_become_damage_evidence(self):
        for text in ('Chance to avoid damage increased by $s1%.',
                     'When allies deal damage, they gain a shield for $s1% of the damage dealt.',
                     'Your attack deals damage and generates $s1 Energy.'):
            r=row(text)
            r['effects'][0]['subtype']=4
            self.assertEqual(evidence.damage_parts(r),[])

    def test_mixed_talent_does_not_include_explicit_healing_component(self):
        r=row('Fireball damage increased by $s1%. Healing increased by $s2%.')
        r['effects'].append({**r['effects'][0],'index':2,'id':102,'flags':[2,0,0,0]})
        self.assertEqual(evidence.damage_parts(r),[1])


if __name__=='__main__':unittest.main()
