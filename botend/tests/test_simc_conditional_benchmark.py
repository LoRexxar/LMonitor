"""Public intent -> reviewed central contract -> ordinary Benchmark plan.

No simulator, network, or mocked trust/eligibility services.
"""
from copy import deepcopy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import TestCase

from botend.models import WowItemSnapshot
from botend.services.simc_benchmark_config import (
    _freeze_case_candidates, _normalize_candidate_params,
)
from botend.services.simc_conditional_store import META_KEY
from botend.tests import test_simc_conditional_store as store_fixtures


class ConditionalBenchmarkTests(TestCase):
    def setUp(self):
        store = store_fixtures.ConditionalStoreTests()
        store.setUp()
        self.contract_key = store.import_source()
        self.owner, self.policy, self.expectation = store.owner, store.policy, store.expectation
        raw = json.loads(gzip.decompress((Path(__file__).with_name('fixtures') /
            'conditional_run2_evidence.json.gz').read_bytes()))['sides']['normal']['params']
        self.public = {'candidate_type': 'gear_swap', 'is_base': False,
                       'gear_swaps': deepcopy(raw['gear_swaps']),
                       'conditional_comparison': {'owner_item_id': self.owner,
                                                  'contract_key': self.contract_key}}

    def plan(self, params):
        normalized = _normalize_candidate_params('gear_swap', params)
        candidate = SimpleNamespace(key='conditional', label='Combination',
            candidate_type='gear_swap', params=normalized, icon_url='',
            source_label='', effect='', display_order=0)
        return _freeze_case_candidates('monk_brewmaster', [candidate])

    def test_public_reference_freezes_reviewed_roles_without_client_authority(self):
        rows = self.plan(self.public)
        normal, control = rows[1:]
        params = normal['candidate_params']
        self.assertEqual(params['equipment_effect_policy'], self.policy)
        self.assertEqual(params['equipment_effect_expectation'], self.expectation)
        self.assertNotIn('conditional_comparison', params)
        self.assertNotIn('conditional_authorization', params)
        self.assertEqual(control['candidate_key'], params['effect_baseline_key'])
        self.assertTrue(control['candidate_params']['equipment_effect_control'])
        self.assertEqual(control['candidate_params']['equipment_effect_policy'], self.policy)
        self.assertEqual(control['candidate_params']['equipment_effect_expectation'], self.expectation)
        self.assertIn('固定', normal['candidate_label'])
        self.assertIn('条件增量', normal['candidate_label'])
        # Editor/API round trip keeps the reference, not the generated contract.
        normalized = _normalize_candidate_params('gear_swap', self.public)
        self.assertEqual(_normalize_candidate_params('gear_swap', normalized), normalized)
        for forbidden in ('equipment_effect_policy', 'equipment_effect_expectation',
                          'conditional_authorization', 'effect_baseline_key', 'pair_binding'):
            malicious = deepcopy(self.public)
            malicious[forbidden] = {}
            with self.subTest(forbidden=forbidden), self.assertRaises(ValidationError):
                self.plan(malicious)
        # No reference retains v2's BOTH-removed interpretation and distinct key.
        legacy = deepcopy(self.public)
        legacy.pop('conditional_comparison')
        old = self.plan(legacy)[1]['candidate_params']
        self.assertEqual(old['equipment_effect_policy']['version'], 2)
        from botend.services.simc_benchmark_execution import _candidate_input_identity
        self.assertNotEqual(_candidate_input_identity({'candidate_params': old}),
                            _candidate_input_identity({'candidate_params': params}))

    def test_bad_reference_or_carrier_never_downgrades_to_v2(self):
        changes = [None, {}, {'owner_item_id': True, 'contract_key': self.contract_key},
                   {'owner_item_id': self.owner, 'contract_key': '0' * 64},
                   {'owner_item_id': self.owner, 'contract_key': self.contract_key, 'approved': True}]
        for value in changes:
            raw = deepcopy(self.public)
            raw['conditional_comparison'] = value
            with self.subTest(reference=value), self.assertRaises(ValidationError):
                self.plan(raw)
        for mutate in ('carrier', 'bonus', 'slot'):
            raw = deepcopy(self.public)
            if mutate == 'carrier':
                row = raw['gear_swaps'][0]
                row['raw_value'] = row['raw_value'].replace(f'id={row["item_id"]}', 'id=999999')
                row['item_id'] = 999999
            elif mutate == 'bonus':
                raw['gear_swaps'][0]['raw_value'] = ','.join(
                    part for part in raw['gear_swaps'][0]['raw_value'].split(',')
                    if not part.startswith('bonus_id='))
            else:
                raw['gear_swaps'][0]['slot'] = 'chest'
            with self.subTest(mutate=mutate), self.assertRaises(ValidationError):
                self.plan(raw)
        owner = WowItemSnapshot.objects.get(item_id=self.owner)
        original = deepcopy(owner.metadata)
        owner.metadata[META_KEY][self.contract_key]['source_review']['source_fact_hash'] = '0' * 64
        owner.save(update_fields=['metadata'])
        with self.assertRaises(ValidationError):
            self.plan(self.public)
        owner.metadata = original
        owner.metadata[META_KEY].clear()
        owner.save(update_fields=['metadata'])
        with self.assertRaises(ValidationError):
            self.plan(self.public)
