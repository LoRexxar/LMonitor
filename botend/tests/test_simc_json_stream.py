import json,math
from unittest import TestCase,mock

class BoundedJsonStreamTests(TestCase):
    def encode(self,value,**kwargs):
        from botend.services.simc_json_stream import iter_json_text
        return ''.join(iter_json_text(value,**kwargs))

    def test_exact_stdlib_json_for_native_numbers_unicode_keys_and_nested_rows(self):
        values=[None,True,False,0,-1,1.25,-0.0,'中文😀\n\\"',[],{},
                {'a':[1,None,{'nested':['汉字',1e-27]}],3:'integer-key',False:'boolean-key',None:'null-key'},
                {'actions':[{'token':str(n),'scenarios':[{'value':n/7,'conditions':[{'token':'buff','stacks':s} for s in range(4)]} for _ in range(15)]} for n in range(90)]}]
        for value in values:
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(self.encode(value),json.dumps(value,ensure_ascii=False,separators=(',',':')))

    def test_aggregate_graph_never_becomes_one_giant_fragment(self):
        from botend.services.simc_json_stream import iter_json_text
        value={'actions':[{'token':'技能','scenarios':[{'n':n,'conditions':[{'scope':'player','stacks':s} for s in range(8)]} for n in range(2000)]}]}
        parts=list(iter_json_text(value))
        self.assertLess(max(map(len,parts)),200000)
        self.assertGreater(len(parts),10)
        self.assertEqual(''.join(parts),json.dumps(value,ensure_ascii=False,separators=(',',':')))

    def test_c_encoder_fast_path_is_used_without_changing_input(self):
        import copy
        from botend.services.simc_json_stream import iter_json_text
        value={'rows':[{'n':n,'name':'中文'} for n in range(2000)]};original=copy.deepcopy(value)
        implementation=json.JSONEncoder.iterencode;flags=[]
        def record(encoder,obj,_one_shot=False):
            flags.append(_one_shot);return implementation(encoder,obj,_one_shot=_one_shot)
        with mock.patch.object(json.JSONEncoder,'iterencode',record):
            result=''.join(iter_json_text(value))
        self.assertTrue(flags and all(flags))
        self.assertEqual(value,original);self.assertEqual(json.loads(result),value)

    def test_cycles_and_invalid_types_still_fail(self):
        value=[];value.append(value)
        for invalid in (value,{'bad':object()}):
            with self.assertRaises((ValueError,TypeError)):
                self.encode(invalid)

    def test_split_dictionary_keys_and_ascii_policy_match_stdlib(self):
        value={n:'中文😀' for n in range(1300)}
        value[None]='none';value[False]='false';value[1.5]='float'
        for ascii_policy in (True,False):
            self.assertEqual(self.encode(value,ensure_ascii=ascii_policy),json.dumps(value,ensure_ascii=ascii_policy,separators=(',',':')))

    def test_nan_policy_and_long_scalar_match_stdlib(self):
        self.assertEqual(self.encode({'n':float('nan')}),'{"n":NaN}')
        with self.assertRaises(ValueError):self.encode({'n':float('nan')},allow_nan=False)
        self.assertEqual(self.encode({'text':'汉😀'*100000}),json.dumps({'text':'汉😀'*100000},ensure_ascii=False,separators=(',',':')))
