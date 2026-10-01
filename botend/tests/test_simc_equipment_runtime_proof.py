"""Native preparation evidence must survive both execution result paths."""
from copy import deepcopy
import json
from unittest.mock import patch
from django.test import SimpleTestCase

import simc_equipment_control as native
from botend.tests.test_simc_equipment_native_gate import NativeEquipmentEffectGateTests
from botend.tests.test_simc_equipment_result_evidence import native_html
from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report


class NativeRuntimeProofTests(SimpleTestCase):
    def prepared(self, control=False):
        fixture = NativeEquipmentEffectGateTests()
        fixture.setUp()
        return fixture.prepare('neck=,id=100001,ilevel=321', ['neck'], control)

    def params(self, control=False):
        return {'candidate_type': 'gear_swap', 'gear_swap': {'slot': 'neck', 'item_id': 100001,
                'raw_value': ',id=100001,ilevel=321'}, 'equipment_effect_policy':
                {'version': 2, 'target_slots': ['neck'], 'rules': native.equipment_rules()},
                'equipment_effect_control': control}

    def test_new_direct_noexpect_uses_native_structure_not_proc_rows(self):
        code = self.prepared()
        proof = native.extract_native_proof(code) if hasattr(native, 'extract_native_proof') else None
        kwargs = {'native_proof': proof} if proof is not None else {}
        result = validate_equipment_effect_report(native_html(profile='neck=,id=100001,ilevel=321',
                    damage=0, triggers=0, uptime=0), self.params(), **kwargs)
        self.assertEqual(result['status'], 'valid', result)
        self.assertEqual(result['validation_basis'], 'native_structure')
        self.assertNotEqual(result['event_status'], 'triggered')

    def test_proof_scope_static_background_and_report_fail_closed(self):
        proof = native.extract_native_proof(self.prepared())
        html = native_html(profile='neck=,id=100001,ilevel=321')
        for mutate in (
            lambda p: p.update(mode='control'),
            lambda p: p.update(scope='other'),
            lambda p: p['targets'][0].update(item_id=2),
            lambda p: p['normal']['items']['neck']['static']['stats'].update(haste=99),
            lambda p: p['control']['items']['neck']['effects'].append({'source': 'item', 'type': 'equip', 'driver': 9}),
            lambda p: p['normal']['items']['neck'].update(effects=[]),
        ):
            changed = deepcopy(proof)
            mutate(changed)
            self.assertEqual(validate_equipment_effect_report(html, self.params(), native_proof=changed)['status'], 'invalid')
        self.assertEqual(validate_equipment_effect_report(native_html(profile='neck=,id=2'), self.params(), native_proof=proof)['status'], 'invalid')
        self.assertEqual(validate_equipment_effect_report(html, self.params())['status'], 'unverified')
        for text in (native.native_proof_marker(proof) * 2, native.NATIVE_PROOF_MARKER + '!' * 90000,
                     native.NATIVE_PROOF_MARKER + '{"schema_version":1,"schema_version":1}'):
            with self.assertRaises(ValueError):
                native.extract_native_proof(text)

    def test_control_and_central_bonus_driver_cannot_be_bypassed(self):
        code = self.prepared(True)
        proof = native.extract_native_proof(code)
        profile = next(line for line in code.splitlines() if line.startswith('neck='))
        result = validate_equipment_effect_report(native_html(profile=profile), self.params(True), native_proof=proof)
        self.assertEqual(result['status'], 'valid', result)
        params = self.params()
        params['equipment_effect_expectation'] = {'schema_version': 1, 'targets': [{
            'slot': 'neck', 'item_id': 100001, 'game_build': '12.0.1.70077', 'fact_hash': 'a' * 64,
            'required_bonus_ids': [777], 'driver_spell_ids': [900001], 'event_spell_ids': [9003]}]}
        normal = native.extract_native_proof(self.prepared())
        self.assertEqual(validate_equipment_effect_report(native_html(profile='neck=,id=100001,ilevel=321'),
                         params, native_proof=normal)['status'], 'invalid')
        params['equipment_effect_expectation']['targets'][0].update(required_bonus_ids=[], driver_spell_ids=[777])
        self.assertEqual(validate_equipment_effect_report(native_html(profile='neck=,id=100001,ilevel=321'),
                         params, native_proof=normal)['status'], 'invalid')
