"""Real native excerpts: item origin survives shared callback ownership changes."""
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from unittest import TestCase

import simc_equipment_control as native
from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
from botend.tests.test_simc_equipment_result_evidence import native_html


class NativeOriginTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(Path(__file__).with_name('fixtures').joinpath('simc_native_origin.json').read_text())

    def prepare(self, run='181888', mutate=None, expectation=None):
        groups = deepcopy(self.fixtures[run])
        if mutate:
            mutate(groups)
        original = self.fixtures[run]['original']['profile']
        control = run == '181895'
        code = native.mark_equipment_input('warrior=MID2_Warrior_Arms\n' + original,
            ['wrists'], control=control, rules=native.equipment_rules(), expectation=expectation)
        def execute(command):
            options = dict(arg.split('=', 1) for arg in command[2:])
            stage = 'original' if command[1].endswith('-original.simc') else (
                'normal' if command[1].endswith('-normal-prepared.simc') else 'control')
            Path(options['save']).write_text(groups[stage]['profile'])
            Path(options['output']).write_text(groups[stage]['log'])
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as directory:
            return native.extract_native_proof(native.prepare_control_input(code, 'simc', directory, execute=execute))

    def params(self, control=False):
        profile = self.fixtures['181888']['original']['profile']
        raw = next(l.partition('=')[2] for l in profile.splitlines() if l.startswith('wrists='))
        return {'candidate_type': 'gear_swap', 'gear_swap': {'slot': 'wrists', 'item_id': 237834, 'raw_value': raw},
            'equipment_effect_policy': {'version': 2, 'target_slots': ['wrists'], 'rules': native.equipment_rules()},
            'equipment_effect_control': control}

    def report(self, proof):
        return native_html(profile='\n'.join(f'{slot}={item["profile_value"]}'
            for slot, item in proof[proof['mode']]['items'].items()))

    def test_real_shared_origin_preparation_and_persisted_validation(self):
        for run in self.fixtures:
            with self.subTest(run=run):
                proof = self.prepare(run)
                self.assertEqual(proof['schema_version'], 2)
                self.assertEqual(proof['background_removed'], ['feet'])
                before = proof['original']['items']['wrists']['effects'][0]
                after = proof['normal']['items']['wrists']['effects'][0]
                self.assertEqual((before['driver'], after['driver']), (1283697, 1229511))
                self.assertEqual((before['trigger'], after['trigger']), (1229511, 1229511))
                self.assertEqual(before['origin'], after['origin'])
                self.assertEqual(before['origin']['driver'], 1283697)
                self.assertFalse(proof['control']['items']['wrists']['effects'])
                result = validate_equipment_effect_report(self.report(proof), self.params(run == '181895'), native_proof=proof)
                self.assertEqual(result['status'], 'valid', result)

    def test_incomplete_or_ambiguous_native_chain_cannot_authorize_migration(self):
        def edit(stage, old, new):
            return lambda g: g[stage].update(log=g[stage]['log'].replace(old, new))
        adding = "item 'spellbreakers_bracers' adding effect 1283697 (type=equip, index=0)"
        init = 'Initializing item-based special effect arcanoweave_lining type=equip source=item driver=1283697 trigger=1229511'
        for mutate in (
            edit('original', adding, 'removed'),
            edit('normal', adding, adding.replace('spellbreakers_bracers', 'other_item')),
            edit('normal', adding, adding.replace('1283697', '999999')),
            edit('normal', 'name=spellbreakers_bracers id=237834', 'name=spellbreakers_bracers id=999999'),
            edit('normal', init, init.replace('driver=1283697 ', '')),
            edit('normal', init, init.replace('trigger=1229511', '')),
            edit('normal', init, ''),
            edit('normal', init, init.replace('1229511', '999999')),
            edit('normal', init, init.replace('source=item', 'source=gem')),
            edit('normal', init, init + '\n0.000 ' + init.replace('1229511', '999999')),
            edit('normal', adding, adding + '\n0.000 Player MID2_Warrior_Arms ' + adding.replace('index=0', 'index=1')),
            edit('normal', "special effects for Player 'MID2_Warrior_Arms'", "special effects for Player 'other_actor'"),
            edit('normal', 'effect={ arcanoweave_lining type=equip source=item driver=1229511 trigger=1229511 proc_chance=101% rppm=2 }', ''),
            edit('normal', 'source=item driver=1229511 trigger=1229511', 'source=item driver=999999 trigger=1229511'),
            edit('normal', 'source=item driver=1229511 trigger=1229511', 'source=item driver=1229511 trigger=999999'),
            edit('normal', 'source=item driver=1229511 trigger=1229511', 'source=item trigger=1229511'),
            edit('normal', init, init + "\n0.000 Initializing special effects for Player 'other_actor'."),
            edit('normal', 'source=item driver=1229511 trigger=1229511', 'source=item driver=1229511'),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.prepare(mutate=mutate)


    def test_schema1_does_not_gain_unproven_driver_equivalence(self):
        proof = self.prepare()
        proof['schema_version'] = 1
        for group in ('original', 'normal', 'control'):
            proof[group].pop('effect_log')
            for item in proof[group]['items'].values():
                item['effects'] = [{key: effect[key] for key in ('source', 'type', 'driver')} for effect in item['effects']]
        self.assertEqual(validate_equipment_effect_report(self.report(proof), self.params(), native_proof=proof)['status'], 'invalid')

    def test_central_declared_driver_requires_origin_and_combat_events_still_required(self):
        expected = {'schema_version': 1, 'targets': [{
            'slot': 'wrists', 'item_id': 237834, 'game_build': '12.1.0.69875', 'fact_hash': 'a' * 64,
            'required_bonus_ids': [12384], 'driver_spell_ids': [1283697], 'event_spell_ids': [1229511]}]}
        proof = self.prepare(expectation=expected)
        params = {**self.params(), 'equipment_effect_expectation': expected}
        result = validate_equipment_effect_report(self.report(proof), params, native_proof=proof)
        self.assertEqual(result['status'], 'unverified', result)
        self.assertIn('equipment_effect_target_events_missing', result['reason_codes'])
        html = self.report(proof).replace('spell=9003', 'spell=1229511')
        self.assertEqual(validate_equipment_effect_report(html, params, native_proof=proof)['status'], 'valid')
        expected['targets'][0]['driver_spell_ids'] = [999999]
        with self.assertRaisesRegex(ValueError, '中央预期特效'):
            self.prepare(expectation=expected)
        self.assertEqual(validate_equipment_effect_report(html, params, native_proof=proof)['status'], 'invalid')

    def test_origin_is_numeric_native_evidence_not_effect_name_or_id_allowlist(self):
        effects = []
        for group in ('original', 'normal'):
            sample = self.fixtures['181888'][group]
            log = sample['log'].replace('1283697', '980001').replace('1229511', '980002')
            log = log.replace('arcanoweave_lining', 'different_' + group)
            item = native.parse_equipment_export(sample['profile'], log, 'wrists')
            effects.append(item['effects'])
        self.assertTrue(native.native_effects_equal(*effects))
        self.assertEqual(effects[1][0]['origin']['driver'], 980001)

    def test_proof_tampering_and_all_existing_gates_remain_closed(self):
        proof = self.prepare()
        for mutate in (
            lambda p: p['normal'].update(effect_log=''),
            lambda p: p['original'].update(effect_log=''),
            lambda p: p['normal']['items']['wrists']['effects'][0].update(driver=999999),
            lambda p: p['normal']['items']['wrists']['effects'][0].update(trigger=999999),
            lambda p: p['normal']['items']['wrists']['effects'][0]['origin'].update(driver=999999),
            lambda p: p['normal']['items']['wrists']['static']['stats'].update(armor=999),
            lambda p: p['control']['items']['wrists'].update(effects=p['normal']['items']['wrists']['effects']),
            lambda p: p['control']['items']['neck']['static']['stats'].update(crit=999),
            lambda p: p['normal']['sets'].append(['unknown_set', 2]),
        ):
            changed = deepcopy(proof)
            mutate(changed)
            result = validate_equipment_effect_report(self.report(changed), self.params(), native_proof=changed)
            self.assertEqual(result['status'], 'invalid', result)

    def test_ordinary_effect_and_schema1_compatibility(self):
        from botend.tests.test_simc_equipment_runtime_proof import NativeRuntimeProofTests
        fixture = NativeRuntimeProofTests()
        proof = native.extract_native_proof(fixture.prepared())
        self.assertEqual(proof['original']['items']['neck']['effects'][0]['driver'], 900001)
        # Freeze a legacy view, never add origins to or mutate historical proof.
        proof['schema_version'] = 1
        for group in ('original', 'normal', 'control'):
            proof[group].pop('effect_log', None)
            for item in proof[group]['items'].values():
                item['effects'] = [{key: effect[key] for key in ('source', 'type', 'driver')} for effect in item['effects']]
        before = deepcopy(proof)
        result = validate_equipment_effect_report(native_html(profile='neck=,id=100001,ilevel=321'),
            fixture.params(), native_proof=proof)
        self.assertEqual(result['status'], 'valid', result)
        self.assertEqual(proof, before)
