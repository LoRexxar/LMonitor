"""Real JSON I/O; only the native generation boundary is substituted."""
import io
import json
import math
from pathlib import Path
import tempfile
import tracemalloc
from unittest import TestCase, mock

from django.core.management import call_command
from django.test import TestCase as DjangoTestCase


class ShortReads(io.StringIO):
    def __init__(self, text, limit):
        super().__init__(text)
        self.limit = limit
        self.requests = []

    def read(self, size=-1):
        if not 0 < size <= 65536:
            raise AssertionError(f'unbounded read: {size}')
        self.requests.append(size)
        return super().read(min(size, self.limit))


class ProfileProductReaderTests(TestCase):
    def read(self, text, limit=65536):
        from botend.services.simc_profile_product_io import read_profile_product
        stream = ShortReads(text, limit)
        result = read_profile_product(stream)
        self.assertTrue(stream.requests)
        return result

    def assert_exact(self, actual, expected):
        self.assertIs(type(actual), type(expected))
        if isinstance(expected, float):
            if math.isnan(expected):
                self.assertTrue(math.isnan(actual))
            else:
                self.assertEqual(actual, expected)
                self.assertEqual(math.copysign(1, actual), math.copysign(1, expected))
        elif isinstance(expected, dict):
            self.assertEqual(list(actual), list(expected))
            for key in expected:
                self.assert_exact(actual[key], expected[key])
        elif isinstance(expected, list):
            self.assertEqual(len(actual), len(expected))
            for a, e in zip(actual, expected):
                self.assert_exact(a, e)
        else:
            self.assertEqual(actual, expected)

    def test_stdlib_types_order_duplicate_keys_and_special_scalars_at_every_small_chunk(self):
        text = (' \n[{"z":0,"a":[true,false,null,0,-0,-0.0,1.0,1e-20,1e400,'
                '-1e400,9007199254740993,123456789012345678901234567890,'
                'NaN,Infinity,-Infinity,"中文😀","\\ud800","\\udfff",'
                '"\\ud83d\\ude00","a\\\"b\\\\c\\n",{},[]],"z":9},'
                '[{"why":"unknown","why":"最后","first":null}],12]\t\r\n')
        expected = json.loads(text)
        for limit in (1, 2, 3, 5, 7, 11, 17, 65536):
            with self.subTest(chunk=limit):
                self.assert_exact(self.read(text, limit), expected)

    def test_rejects_bad_envelope_truncation_invalid_json_and_trailing_data(self):
        invalid = [
            '', 'null', '{}', '"text"', '[]', '[{},[]]', '[{},[],0,4]',
            '[{},[],0]false', '[{},[],0] []', '[{},[],0],',
            '[{},[],0', '[{},[],0,]', '[{},[],0.]', '[{},[],01]',
            '[{},[],1e]', '[{},[],+1]', '[{},[],.1]', '[{},[],nan]',
            '[{},[],infinity]', '[{},[],--1]', '[{},[],true false]',
            '[{"a":1,},[],0]', '[{"a" 1},[],0]', '[{1:2},[],0]',
            '[{"a":"bad\\x"},[],0]', '[{"a":"bad\\u12"},[],0]',
            '[{"a":"raw\x01"},[],0]', '[{"a":"unterminated},[],0]',
            '[{"a":[1,]},[],0]', '\ufeff[{},[],0]',
            '[{},[],0]\u00a0', '[{},[],0]/*comment*/',
        ]
        for text in invalid:
            for limit in (1, 7, 65536):
                with self.subTest(text=repr(text), chunk=limit):
                    with self.assertRaises(ValueError):
                        self.read(text, limit)

    def test_document_keys_are_shared_across_manual_and_decoder_objects(self):
        from botend.services.simc_profile_product_io import _JSONReader
        key = 'repeated_profile_field_name_not_interned_' * 3
        # The first/last objects fit in a decoder window; the middle object's
        # large value requires the manual object path, across multiple windows.
        text = json.dumps([{key: 1, 'rows': [{key: 'x' * 70000}, {key: 2}]},
                           [{key: 3}], 0])
        for limit in (17, 65536):
            with self.subTest(chunk=limit):
                reader = _JSONReader(ShortReads(text, limit))
                product = reader.read()
                keys = [next(iter(product[0])),
                        *(next(iter(row)) for row in product[0]['rows']),
                        next(iter(product[1][0]))]
                self.assertTrue(all(candidate is keys[0] for candidate in keys))
                # No process-global interning, nor an extra retained key table.
                self.assertEqual(reader.key_memo, {})
                other = self.read(text, limit)
                self.assertIsNot(next(iter(other[0])), keys[0])
                broken = _JSONReader(ShortReads(text + ' false', limit))
                with self.assertRaises(ValueError):
                    broken.read()
                self.assertEqual(broken.key_memo, {})

    def test_long_number_growth_is_geometric_and_matches_stdlib(self):
        from botend.services.simc_profile_product_io import _JSONReader
        growth = []

        class ObservedReader(_JSONReader):
            def _ensure(self, count):
                if count > len(self.buffer) - self.pos and not self.eof:
                    growth.append(count)
                return super()._ensure(count)

        number = '0.' + '1234567890' * 60000
        text = '[{},[],' + number + ']'
        self.assertEqual(ObservedReader(ShortReads(text, 65536)).read(), json.loads(text))
        # Bound cumulative text requests rather than machine-dependent timings.
        # One-more-character growth copies the entire token once per read block.
        self.assertLess(sum(growth), 4 * len(number))

    def test_repeated_long_keys_retain_near_stdlib_graph_memory(self):
        import gc
        from botend.services.simc_profile_product_io import read_profile_product
        row = {f'profile_repeated_long_field_{n}_not_interned': n for n in range(8)}
        text = json.dumps([{'rows': [row] * 6000}, [], 0], separators=(',', ':'))
        stream = io.StringIO(text)

        def retained(read):
            gc.collect()
            tracemalloc.start()
            try:
                product = read()
                gc.collect()
                current, _ = tracemalloc.get_traced_memory()
                self.assertEqual(len(product[0]['rows']), 6000)
                return current
            finally:
                tracemalloc.stop()

        stdlib_bytes = retained(lambda: json.loads(text))
        reader_bytes = retained(lambda: read_profile_product(stream))
        self.assertLess(reader_bytes, stdlib_bytes * 1.3 + 65536,
                        (stdlib_bytes, reader_bytes))

    def test_deep_large_scalar_seekable_fallback_matches_stdlib_from_start_offset(self):
        from botend.services import simc_profile_product_io as product_io
        text = '[' + '[' * 500 + '"' + 'x' * 70000 + '"' + ']' * 500 + ',[],0]'
        # Establish that this is legal at the unchanged interpreter recursion limit.
        expected = json.loads(text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deep.json'
            for prefix in ('', 'ignored 中文😀 prefix\n'):
                for kind in ('StringIO', 'file'):
                    with self.subTest(prefix=prefix, kind=kind):
                        path.write_text(prefix + text, encoding='utf-8')
                        stream = (io.StringIO(prefix + text) if kind == 'StringIO'
                                  else path.open(encoding='utf-8'))
                        with stream:
                            stream.read(len(prefix))
                            start = stream.tell()
                            with mock.patch.object(stream, 'read', wraps=stream.read) as reads:
                                actual = product_io.read_profile_product(stream)
                            # The compatibility path re-reads the remaining
                            # document once; normal bounded reads use a size.
                            self.assertEqual(sum(not call.args for call in reads.call_args_list), 1)
                            self.assertGreater(stream.tell(), start)
                            value, wanted = actual[0], expected[0]
                            for _ in range(500):
                                self.assertEqual(len(value), 1)
                                value, wanted = value[0], wanted[0]
                            self.assertEqual(value, wanted)
                            self.assertEqual(actual[1:], [[], 0])
            path.write_text(text, encoding='utf-8')
            self.assertEqual(product_io.read_profile_product(path)[2], 0)

    def test_deep_fallback_preserves_malformed_and_trailing_rejection(self):
        from botend.services.simc_profile_product_io import read_profile_product
        text = '[' + '[' * 500 + '"' + 'x' * 70000 + '"' + ']' * 500 + ',[],0]'
        for malformed in (text[:-1], text + ' false', text + ' []',
                          text.replace(',[],0]', ',[],01]'),
                          text.replace('"' + ']' * 500, '"' + ']' * 499)):
            with self.subTest(ending=malformed[-40:]):
                with self.assertRaises(ValueError):
                    json.loads(malformed)
                with self.assertRaises(ValueError):
                    read_profile_product(io.StringIO(malformed))

    def test_nonseekable_stream_keeps_bounded_reads_and_explicit_depth_limit(self):
        from botend.services.simc_profile_product_io import read_profile_product

        class NonSeekable(ShortReads):
            def seekable(self):
                return False

            def tell(self):
                raise io.UnsupportedOperation('not seekable')

            def seek(self, *args):
                raise io.UnsupportedOperation('not seekable')

        text = '[{"rows":[' + ','.join('{"field":1}' for _ in range(10000)) + ']},[],0]'
        self.assertEqual(len(read_profile_product(NonSeekable(text, 8191))[0]['rows']), 10000)
        deep = '[' + '[' * 500 + '"' + 'x' * 70000 + '"' + ']' * 500 + ',[],0]'
        with self.assertRaises(RecursionError):
            read_profile_product(NonSeekable(deep, 65536))

    def test_large_scalar_and_int_digit_limit_keep_stdlib_semantics(self):
        text = json.dumps([{'text': '汉😀\\"' * 50000}, [], -0.0], ensure_ascii=False)
        self.assert_exact(self.read(text, 8191), json.loads(text))
        import sys
        digits = sys.get_int_max_str_digits()
        if digits:
            text = '[{},[],' + '9' * (digits + 1) + ']'
            with self.assertRaises(ValueError):
                json.loads(text)
            with self.assertRaises(ValueError):
                self.read(text, 17)

    def test_actual_file_preserves_utf8_policy_and_escaped_surrogates(self):
        from botend.services.simc_profile_product_io import read_profile_product
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            path.write_bytes(b'[{"text":"\\ud800"},[],0]')
            self.assertEqual(read_profile_product(path), [{'text': '\ud800'}, [], 0])
            for raw in (b'[{"text":"\xed\xa0\x80"},[],0]', b'[{},[],0]\xff'):
                path.write_bytes(raw)
                with self.assertRaises(UnicodeDecodeError):
                    read_profile_product(path)

    def test_writer_special_values_exact_bytes_and_stdlib_failures(self):
        from botend.services.simc_profile_product_io import write_profile_product
        value = ({'keys': {None: 'null-key', False: 'boolean-key', 1.5: 'float-key'},
                  'values': [True, False, None, 10 ** 80, -0.0, float('nan'),
                             float('inf'), -float('inf'), '中文😀\\n']}, [], 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            write_profile_product(path, value)
            self.assertEqual(path.read_bytes(), json.dumps(
                value, ensure_ascii=False, separators=(',', ':'),
            ).encode('utf-8'))
            cycle = []
            cycle.append(cycle)
            for invalid in ([{'text': '\ud800'}, [], 0], [{'bad': object()}, [], 0], cycle):
                try:
                    json.dumps(invalid, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                except (ValueError, TypeError, UnicodeEncodeError) as expected:
                    with self.subTest(error=type(expected).__name__):
                        with self.assertRaises(type(expected)):
                            write_profile_product(path, invalid)
                else:
                    self.fail('fixture should be rejected by the original writer')

    def test_every_truncation_is_rejected_and_large_object_duplicate_order_matches(self):
        text = '[{"rows":[1,-0.0,"中文\\\\\\\"",{"a":[true,null]}]},[{"why":"unknown"}],50]'
        for end in range(len(text)):
            with self.subTest(end=end):
                with self.assertRaises(ValueError):
                    self.read(text[:end], 7)
        text = '[{"first":0,' + ','.join(
            f'"key{n}":{{"n":{n},"value":1e-40}}' for n in range(2500)
        ) + ',"first":99},[],2500]'
        self.assertGreater(len(text), 65536)
        self.assert_exact(self.read(text), json.loads(text))

    def test_reader_does_not_retain_a_document_sized_text_copy(self):
        from botend.services.simc_profile_product_io import read_profile_product
        from botend.services.simc_json_stream import iter_json_text
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            # Enough real text to distinguish a whole-document temporary from
            # bounded parser buffers, without native runs or large artifacts.
            with path.open('w', encoding='utf-8') as output:
                output.write('[{"actions":[')
                for n in range(6000):
                    if n:
                        output.write(',')
                    output.writelines(iter_json_text({'id': n, 'text': 'x' * 400}))
                output.write(']},[],6000]')
            self.assertGreater(path.stat().st_size, 2_000_000)
            tracemalloc.start()
            try:
                decoded = read_profile_product(path)
                current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(len(decoded[0]['actions']), 6000)
            self.assertEqual(decoded[0]['actions'][-1]['id'], 5999)
            self.assertLess(peak - current, 1_000_000)


class ProfileProductCommandTests(DjangoTestCase):
    def test_real_profile_command_writes_bounded_fragments_and_exact_wire_bytes(self):
        from botend.models import SimcProfile, SimcSkillDamageSnapshot
        from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='e' * 40, game_build='12.1.0.70000', schema_revision=45,
        )
        profile = SimcProfile.objects.create(name='wire fixture', class_name='warrior', spec='fury')
        result = ({'class': 'warrior', 'specialization': 'fury', 'actions': [
            {'id': n, 'text': '中文😀', 'value': n / 7, 'negative_zero': -0.0}
            for n in range(6000)
        ]}, [{'reason': 'unknown', 'value': None, 'bool': False}], 6000)
        original_open = Path.open
        sizes = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'

            class ObservedOutput:
                def __init__(self, output):
                    self.output = output

                def __enter__(self):
                    self.output.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.output.__exit__(*args)

                def write(self, text):
                    sizes.append(len(text))
                    if len(text) > 200000:
                        raise AssertionError('whole profile JSON text reached the file writer')
                    return self.output.write(text)

                def writelines(self, fragments):
                    for fragment in fragments:
                        self.write(fragment)

            def observed_open(candidate, *args, **kwargs):
                output = original_open(candidate, *args, **kwargs)
                if candidate == path and args and args[0] == 'w':
                    return ObservedOutput(output)
                if candidate == path and kwargs.get('mode') == 'w':
                    return ObservedOutput(output)
                return output

            # Only native generation is a fixture; ORM lookup, management
            # command, encoder, UTF-8 file, and decoder are all real.
            with mock.patch.object(SimcSkillDamageSnapshotService, '_generate_profile_product_actor',
                                   return_value=result), mock.patch.object(Path, 'open', observed_open):
                call_command('generate_simc_skill_damage_snapshot', snapshot_id=snapshot.pk,
                             profile_id=profile.pk, output=str(path))
            expected = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.assertEqual(path.read_bytes(), expected)
            self.assertGreater(len(sizes), 10)
            from botend.services.simc_profile_product_io import read_profile_product
            self.assertEqual(read_profile_product(path), json.loads(expected))
