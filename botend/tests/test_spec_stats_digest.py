"""Exact canonical-byte compatibility and bounded-memory hashing."""
import hashlib,json,math,random,tracemalloc
from datetime import date,datetime,timezone
from decimal import Decimal
from django.test import SimpleTestCase
from botend.services.spec_stats_snapshot import content_digest,_bounded_canonical_chunks

def encoder():
    def default(value):
        if isinstance(value,Decimal):return float(value)
        if isinstance(value,(date,datetime)):return value.isoformat()
        raise TypeError(type(value).__name__)
    return json.JSONEncoder(sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False,default=default)

def legacy_digest(value):
    result=hashlib.sha256()
    for text in encoder().iterencode(value):result.update(text.encode('utf-8'))
    return result.hexdigest()

class CanonicalDigestTests(SimpleTestCase):
    def test_exact_bytes_across_forced_chunk_boundaries(self):
        cases=[None,False,123,-0.0,1e-20,{},[],('中文','😀','\\\"\n'),
            {'z':[1,2,{'雪':'a\t\b\u0000'}],'a':Decimal('1.23456789012345678'),'d':date(2026,10,25),'dt':datetime(2026,10,25,tzinfo=timezone.utc)},
            {1:'one',2:'two',-3:'negative'}, {None:'null'}, {True:'true'}, {2.5:'float'},
            [{'id':i,'data':list(range(10)),'s':'abc'*100} for i in range(20)]]
        for value in cases:
            with self.subTest(value_type=type(value).__name__):
                expected=''.join(encoder().iterencode(value))
                actual=''.join(_bounded_canonical_chunks(value,encoder(),byte_budget=64,node_budget=8))
                self.assertEqual(actual,expected)
                self.assertEqual(content_digest(value),legacy_digest(value))
    def test_random_nested_payloads_preserve_all_bytes(self):
        rng=random.Random(5047)
        def make(depth):
            if depth<=0:return rng.choice([None,True,-0.0,17,1e30,'🌟中文\n'])
            return {str(i):[make(depth-1),make(depth-1)] for i in range(rng.randrange(1,5))}
        for _ in range(12):
            value=make(3)
            self.assertEqual(''.join(_bounded_canonical_chunks(value,encoder(),node_budget=20)),''.join(encoder().iterencode(value)))
    def test_invalid_values_and_cycles_still_fail_closed(self):
        cycle=[];cycle.append(cycle)
        for value in (float('nan'),float('inf'),{float('nan'):1},object(),{object():1},{'a':1,2:3},cycle):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises((TypeError,ValueError)):content_digest(value)
    def test_large_publication_streams_without_full_json_materialization(self):
        value={'rows':[{'id':i,'text':'x'*2048,'values':[i,1.2,None]} for i in range(8000)]}
        expected=legacy_digest(value)
        tracemalloc.start()
        try:
            actual=content_digest(value);_,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertEqual(actual,expected)
        self.assertLess(peak,2*1024*1024)
        self.assertLess(max(len(s.encode('utf-8')) for s in _bounded_canonical_chunks(value,encoder())),256*1024)
    def test_shared_subtrees_are_not_cycles(self):
        shared={'x':list(range(100))};value=[shared]*6000
        self.assertEqual(content_digest(value),legacy_digest(value))
