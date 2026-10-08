"""Frozen Benchmark201 native excerpts; no DB, network or SimC mocks of parsing.

Fixtures capture real prepare probes from local SimC 91eb5c1e (12.1.0.69875),
not the production binary. SHA256 values bind each excerpt to diagnosis logs.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase

import simc_equipment_control as native
from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
from botend.tests.test_simc_equipment_result_evidence import native_html


class Benchmark201NativeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(Path(__file__).with_name('fixtures').joinpath(
            'simc_benchmark201_native.json').read_text())

    def prepare(self, run, mutate=None):
        fixture = deepcopy(self.fixtures[str(run)])
        if mutate:
            mutate(fixture['stages'])

        def execute(command):
            options = dict(arg.split('=', 1) for arg in command[2:])
            stage = ('original' if command[1].endswith('-original.simc') else
                     'normal' if command[1].endswith('-normal-prepared.simc') else 'control')
            Path(options['save']).write_text(fixture['stages'][stage]['profile'])
            Path(options['output']).write_text(fixture['stages'][stage]['log'])
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with tempfile.TemporaryDirectory() as directory:
            return native.extract_native_proof(native.prepare_control_input(
                fixture['code'], 'simc', directory, execute=execute))

    def validate(self, run, proof, control=False):
        params = deepcopy(self.fixtures[str(run)]['params'])
        params['equipment_effect_control'] = control
        proof = deepcopy(proof)
        proof['mode'] = 'control' if control else 'normal'
        profile = '\n'.join(f'{slot}={item["profile_value"]}' for slot, item in
                            proof[proof['mode']]['items'].items())
        # Synthetic HTML only for this contract; actual OSS reports are replayed
        # separately and their hashes recorded in native-findings.md.
        html = native_html(profile=profile, buff_spell=1307927)
        return validate_equipment_effect_report(html, params, native_proof=proof)

    def test_hunter_pet_item_scopes_do_not_discard_proven_player_origin(self):
        proof = self.prepare(183416)
        before = proof['original']['items']['wrists']['effects'][0]
        after = proof['normal']['items']['wrists']['effects'][0]
        self.assertEqual((before['driver'], after['driver']), (1283697, 1229511))
        self.assertIsNotNone(before['origin'])
        self.assertEqual(before['origin'], after['origin'])
        for control in (False, True):
            self.assertEqual(self.validate(183416, proof, control)['status'], 'valid')

    def test_only_explicit_pet_identity_without_competing_effects_is_excluded(self):
        def unknown_actor(stages):
            stages['normal']['log'] = stages['normal']['log'].replace(
                'Creating Auras, Buffs, and Debuffs for Pet ', 'Unknown actor ')

        def pet_effect(stages):
            stages['normal']['log'] += ("\n0.000 Player MID2_Hunter_Beast_Mastery_duck "
                "item 'farstriders_plated_bracers' adding effect 1283697 (type=equip, index=0)")

        for mutate in (unknown_actor, pet_effect):
            with self.subTest(mutate=mutate), self.assertRaisesRegex(ValueError, '原生效果与准备后不一致'):
                self.prepare(183416, mutate)

    def test_dual_wield_background_identity_resolution_is_not_runtime_change(self):
        proof = self.prepare(182238)
        normal = proof['normal']['items']['off_hand']['effects'][0]
        control = proof['control']['items']['off_hand']['effects'][0]
        self.assertIsNone(normal['origin'])
        self.assertIsNotNone(control['origin'])
        self.assertEqual(normal['driver'], control['driver'])
        self.assertEqual(normal['trigger'], control['trigger'])
        for mode in (False, True):
            result = self.validate(182238, proof, mode)
            self.assertEqual(result['status'], 'valid', result)

    def test_background_runtime_and_static_changes_still_rejected(self):
        proof = self.prepare(182238)
        for field, value in (('profile_value', 'other,id=268202'), ('item_id', 1),
                             ('effects', [])):
            changed = deepcopy(proof)
            changed['control']['items']['off_hand'][field] = value
            self.assertEqual(self.validate(182238, changed)['status'], 'invalid')
        changed = deepcopy(proof)
        changed['control']['items']['off_hand']['static']['stats']['haste'] = 999
        self.assertEqual(self.validate(182238, changed)['status'], 'invalid')
