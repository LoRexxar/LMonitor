"""Private v1 transport: pure codec and offline real evidence replay."""
import gzip
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import simc_conditional_evidence as codec


class EvidenceCodecTests(unittest.TestCase):
    def test_original_utf8_roundtrip(self):
        value = {'prepared_input': '角色="测试"\r\n', 'report_json': ' {"n":1.00,"name":"测试"}\n'}
        self.assertEqual(codec.decode(codec.encode(**value)), value)
        self.assertEqual(codec.PROTOCOL_VERSION, 1)

    def test_reject_ambiguous_json_and_gzip(self):
        good = codec.encode('input', '{}')
        envelopes = [b'{}', b'{"prepared_input":"a","report_json":"{}","extra":1}',
            b'{"prepared_input":"a","prepared_input":"b","report_json":"{}"}',
            b'{"prepared_input":"a","report_json":"{}"}{}', b'\xff']
        for raw in envelopes:
            with self.subTest(raw=raw), self.assertRaises(codec.EvidenceError):
                codec.decode(gzip.compress(raw))
        for raw in [good + good, good + b'\x00', good[:-1], b'bad']:
            with self.subTest(raw=raw[:10]), self.assertRaises(codec.EvidenceError):
                codec.decode(raw)
        for report in ['{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', '{"a":1,"a":2}', '{}{}', '[]']:
            with self.subTest(report=report), self.assertRaises(codec.EvidenceError):
                codec.encode('input', report)
            envelope = json.dumps({'prepared_input': 'input', 'report_json': report}).encode()
            with self.assertRaises(codec.EvidenceError):
                codec.decode(gzip.compress(envelope))

    def test_limits_on_both_paths(self):
        for constant, prepared, report in [('MAX_PREPARED_BYTES', 'a' * 33, '{}'),
                ('MAX_REPORT_BYTES', 'a', '{"a":"' + 'x' * 33 + '"}')]:
            raw = gzip.compress(json.dumps({'prepared_input': prepared, 'report_json': report}).encode())
            with patch.object(codec, constant, 32):
                with self.assertRaises(codec.EvidenceError): codec.encode(prepared, report)
                with self.assertRaises(codec.EvidenceError): codec.decode(raw)
        with patch.object(codec, 'MAX_ENVELOPE_BYTES', 100):
            with self.assertRaises(codec.EvidenceError): codec.decode(gzip.compress(b' ' * 1000000))
            with self.assertRaises(codec.EvidenceError): codec.encode('a' * 101, '{}')
        with patch.object(codec, 'MAX_COMPRESSED_BYTES', 10):
            with self.assertRaises(codec.EvidenceError): codec.encode('a', '{}')
            with self.assertRaises(codec.EvidenceError): codec.decode(b'x' * 11)

    def test_real_both_sides_strict_validator_equal(self):
        from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
        from botend.tests.test_simc_conditional_core import fixture
        from simc_equipment_conditional import validate_pair_witness
        sides = json.loads(gzip.decompress((Path(__file__).with_name('fixtures') / 'conditional_run2_evidence.json.gz').read_bytes()))['sides']
        auth = fixture()[2]
        before, after = [], []
        for mode in ('normal', 'control'):
            data = sides[mode]
            # Fixture stores parsed JSON, not the original report file bytes.
            report_text = json.dumps(data['report'], ensure_ascii=False, indent=1) + '\n'
            decoded = codec.decode(codec.encode(data['prepared'], report_text))
            self.assertEqual(decoded['report_json'], report_text)
            self.assertEqual(decoded['prepared_input'], data['prepared'])
            kwargs = dict(native_proof=data['proof'], conditional_authorization=auth)
            original = validate_equipment_effect_report(data['html'], data['params'], prepared_input=data['prepared'], report_json=data['report'], **kwargs)
            restored = validate_equipment_effect_report(data['html'], data['params'], prepared_input=decoded['prepared_input'], report_json=json.loads(decoded['report_json']), **kwargs)
            self.assertEqual(restored, original)
            before.append(original); after.append(restored)
        self.assertEqual(validate_pair_witness(*before), validate_pair_witness(*after))
        self.assertTrue(validate_pair_witness(*after)['valid'])
