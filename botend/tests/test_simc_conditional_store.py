import json
from copy import deepcopy
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import TestCase

from botend.models import WowItemSnapshot
from botend.services.wow_item_effect_activation_store import (
    META_KEY as ACTIVATION_KEY, activation_reference,
)
from botend.services.simc_conditional_store import (
    META_KEY, import_reviewed_contract, approve_executable, freeze_conditional_contract,
)
from simc_equipment_conditional import validate_contract


class ConditionalStoreTests(TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / 'fixtures' /
                                   'conditional_samebuild_contract.json').read_text())
        self.policy = self.fixture['policy']
        self.expectation = self.fixture['expectation']
        relation = self.expectation['relations'][0]
        # Real source/DBC/native evidence from the checked-in same-build fixture;
        # central activation hashes are distinct from the original crafting hash.
        for target in self.expectation['targets']:
            fact = {'schema_version': 1, 'item_id': target['item_id'],
                    'game_build': target['game_build'],
                    'required_bonus_ids': target['required_bonus_ids'],
                    'effects': [{'spell_id': d} for d in target['driver_spell_ids']],
                    'source': {'provider': 'reviewed_simc_dbc',
                               'evidence': relation['source_fact']['dbc_carrier_chain']},
                    'native_event_evidence': [
                        {'item_id': target['item_id'], 'game_build': target['game_build'],
                         'driver_spell_id': target['driver_spell_ids'][0],
                         'event_spell_id': event,
                         'report_sha256': relation['provenance']['report_hashes']['normal']['report.json'],
                         'simc_revision': self.expectation['identity']['revision']}
                        for event in target['event_spell_ids']]}
            target['fact_hash'] = activation_reference(fact)['fact_hash']
            WowItemSnapshot.objects.create(item_id=target['item_id'], slot_key=target['slot'],
                metadata={'untouched': True, ACTIVATION_KEY: {
                    target['game_build']: {'is_ptr': True, 'fact': fact}}})
        self.owner = next(t['item_id'] for t in self.expectation['targets'] if t['role'] == 'changed')
        self.review = {'kind': 'independent_source_review', 'reviewer': 'test-reviewer',
                       'evidence_sha256': relation['provenance']['simc_relation']['sha256'],
                       'source_fact_hash': relation['source_fact']['fact_hash']}
        self.approval = {'kind': 'independent_executable_review', 'reviewer': 'test-reviewer',
                         'evidence_sha256': relation['provenance']['pair_witness']['sha256'],
                         'platform': 'linux', 'identity': self.expectation['identity'],
                         'source_fact_hash': self.review['source_fact_hash'],
                         'relation_hashes': self.fixture['authorization']['relation_hashes']}
        self.selector = {'owner_item_id': self.owner, 'game_build': self.expectation['identity']['dbc_build'],
                         'is_ptr': True, 'identity': self.expectation['identity'], 'platform': 'linux',
                         'relation_hash': relation['relation_hash'],
                         'source_fact_hash': self.review['source_fact_hash']}

    def import_source(self):
        return import_reviewed_contract(policy=self.policy, expectation=self.expectation,
                                        is_ptr=True, review=self.review)

    def approve(self):
        self.import_source()
        return approve_executable(**self.selector, approval=self.approval)

    def test_freeze_is_detached_and_remains_valid_after_live_deletion(self):
        self.approve()
        with self.assertNumQueries(1):
            frozen = freeze_conditional_contract(**self.selector)
        self.assertEqual(set(frozen), {'policy', 'expectation', 'conditional_authorization'})
        self.assertEqual(frozen['conditional_authorization'], self.fixture['authorization'])
        item = WowItemSnapshot.objects.get(item_id=self.owner)
        self.assertTrue(item.metadata['untouched'])
        self.assertIn(ACTIVATION_KEY, item.metadata)
        WowItemSnapshot.objects.all().delete()
        validate_contract(frozen['policy'], frozen['expectation'],
                          authorization=frozen['conditional_authorization'])
        self.assertEqual(frozen['expectation'], self.expectation)

    def test_two_stage_source_does_not_authorize_binary(self):
        self.import_source()
        with self.assertRaises(ValidationError):
            freeze_conditional_contract(**self.selector)
        self.approve()
        self.assertEqual(freeze_conditional_contract(**self.selector)['policy'], self.policy)

    def test_exact_selection_rejects_all_mismatches(self):
        self.approve()
        changes = [{'game_build': '12.1.0.69934'}, {'is_ptr': False}, {'platform': 'windows'},
                   {'relation_hash': '0' * 64}, {'source_fact_hash': '0' * 64},
                   {'identity': {**self.expectation['identity'], 'binary_sha256': '0' * 64}},
                   {'identity': {**self.expectation['identity'], 'revision': '0' * 40}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                freeze_conditional_contract(**{**self.selector, **change})

    def test_import_requires_core_and_central_binding(self):
        original = deepcopy(self.expectation)
        for field, value in [('fact_hash', '0' * 64), ('item_id', 999999),
                             ('driver_spell_ids', [999999])]:
            self.expectation = deepcopy(original)
            self.expectation['targets'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.import_source()
        self.expectation = original
        self.review['source_fact_hash'] = '0' * 64
        with self.assertRaises(ValidationError):
            self.import_source()
        self.assertFalse(any(META_KEY in i.metadata for i in WowItemSnapshot.objects.all()))

    def test_agent_telemetry_cannot_approve_and_hashes_are_not_rederived(self):
        self.import_source()
        bad = deepcopy(self.approval)
        bad['kind'] = 'agent_report'
        with self.assertRaises(ValidationError):
            approve_executable(**self.selector, approval=bad)
        bad = deepcopy(self.approval)
        bad['relation_hashes'] = ['0' * 64]
        with self.assertRaises(ValidationError):
            approve_executable(**self.selector, approval=bad)
        with self.assertRaises(TypeError):
            freeze_conditional_contract(**self.selector, authorization=self.fixture['authorization'])

    def test_source_integrity_and_central_branch_are_checked(self):
        original = deepcopy(self.expectation)
        self.expectation['relations'][0]['source_fact']['source_file']['sha256'] = '0' * 64
        with self.assertRaises(ValidationError):
            self.import_source()
        self.expectation = original
        item = WowItemSnapshot.objects.get(item_id=self.owner)
        item.metadata[ACTIVATION_KEY][self.selector['game_build']]['is_ptr'] = False
        item.save(update_fields=['metadata'])
        with self.assertRaises(ValidationError):
            self.import_source()

    def test_central_approved_hash_is_not_recreated_from_contract(self):
        self.approve()
        item = WowItemSnapshot.objects.get(item_id=self.owner)
        record = next(iter(item.metadata[META_KEY].values()))
        record['executables']['linux']['relation_hashes'] = ['0' * 64]
        item.save(update_fields=['metadata'])
        with self.assertRaises(ValidationError):
            freeze_conditional_contract(**self.selector)

    def test_independent_versions_are_append_only(self):
        self.approve()
        first = freeze_conditional_contract(**self.selector)
        from simc_equipment_conditional import digest
        self.expectation['identity']['binary_sha256'] = '1' * 64
        relation = self.expectation['relations'][0]
        relation['provenance']['binary_sha256'] = '1' * 64
        relation['relation_hash'] = digest({k: v for k, v in relation.items() if k != 'relation_hash'})
        # Test-only alternate artifact is source-reviewed, NEVER executable-approved.
        self.import_source()
        item = WowItemSnapshot.objects.get(item_id=self.owner)
        self.assertEqual(len(item.metadata[META_KEY]), 2)
        old_selector = {**self.selector, 'identity': first['expectation']['identity']}
        self.assertEqual(freeze_conditional_contract(**old_selector), first)
        new_selector = {**self.selector, 'relation_hash': relation['relation_hash']}
        with self.assertRaisesRegex(ValidationError, 'binary'):
            freeze_conditional_contract(**new_selector)

    def test_observed_artifact_unique_no_revision_and_refusal_cases(self):
        from botend.services.simc_conditional_store import freeze_observed_conditional_contract
        args = {k: deepcopy(v) for k, v in self.selector.items() if k != 'identity'}
        args['observation'] = {**self.expectation['identity'], 'revision': None}
        self.import_source()
        with self.assertRaises(ValidationError):
            freeze_observed_conditional_contract(**args)
        self.approve()
        with self.assertNumQueries(1):
            frozen = freeze_observed_conditional_contract(**args)
        self.assertEqual(frozen['expectation'], self.expectation)
        self.assertEqual(frozen['conditional_authorization'], self.fixture['authorization'])
        for changes in ({'platform': 'windows'}, {'is_ptr': False},
                        {'relation_hash': '0' * 64}, {'source_fact_hash': '0' * 64},
                        {'observation': {**args['observation'], 'revision': '0' * 40}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                freeze_observed_conditional_contract(**{**args, **changes})
        item = WowItemSnapshot.objects.get(item_id=self.owner)
        entries = item.metadata[META_KEY]
        entries['duplicate-review-record'] = deepcopy(next(iter(entries.values())))
        item.save(update_fields=['metadata'])
        with self.assertRaisesMessage(ValidationError, 'ambiguous'):
            freeze_observed_conditional_contract(**args)

    def test_idempotent_import_and_readback(self):
        self.approve()
        before = WowItemSnapshot.objects.get(item_id=self.owner).metadata
        self.approve()
        self.assertEqual(WowItemSnapshot.objects.get(item_id=self.owner).metadata, before)
        first = freeze_conditional_contract(**self.selector)
        first['conditional_authorization']['relation_hashes'].clear()
        self.assertTrue(freeze_conditional_contract(**self.selector)['conditional_authorization']['relation_hashes'])
