"""One-call parsing on untouched captured native HTML and full frozen outputs.

The expectation variants reuse the independent activation probe's frozen neck
identities; they are explicit contract inputs, not claimed Exec191 parameters.
No source/parser result is mocked: the spy always executes the real parser.
"""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.services import simc_equipment_result_evidence as native
from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report


class EquipmentReportParsingTests(SimpleTestCase):
    def test_real_native_html_parses_once_and_preserves_entire_output(self):
        fixture = Path(__file__).parent / 'fixtures' / 'simc_equipment_native_parsing.json.gz'
        cases = json.loads(gzip.decompress(fixture.read_bytes()))
        self.assertEqual(len(cases), 4)
        for case in cases:
            with self.subTest(case=case['name']):
                self.assertEqual(hashlib.sha256(case['html'].encode()).hexdigest(), case['input_sha256'])
                params, proof = deepcopy(case['params']), deepcopy(case['proof'])
                # Two independent validations must each parse once, not share a
                # global cache or an output subsequently mutated by a caller.
                for _ in range(2):
                    with patch.object(native, '_native_document', wraps=native._native_document) as parser:
                        result = validate_equipment_effect_report(case['html'], params, native_proof=proof)
                    self.assertEqual(result, case['expected_output'])
                    self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
                    self.assertEqual(parser.call_count, 1)
                    result['targets'].clear()
                self.assertEqual(params, case['params'])
                self.assertEqual(proof, case['proof'])
