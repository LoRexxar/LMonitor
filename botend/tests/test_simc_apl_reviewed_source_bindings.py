"""Unlearned runtime data is not evidence that a real action has no identity."""
import copy
import json
import tempfile
from pathlib import Path
from django.test import TestCase
from botend.models import SimcAplSymbolScope
from botend.services.simc_apl.symbol_sync import build_symbol_facts, sync_symbols

REVISION = '30c4b061f2103132a34952d80c963303b82bf368'
BUILD = '12.1.5.70575'


def runtime_payload():
    return {
        'schema_version': 1, 'simc_revision': REVISION, 'game_build': BUILD,
        'completeness': {'status': 'partial', 'modules': {
            key: 'runtime_initialized' for key in
            ('global_options', 'actions', 'action_options', 'expressions', 'class_specs')},
            'limitations': ['Only initialized talent selections are observed.']},
        'symbols': [dict({'class': 'demonhunter', 'spec': 'devourer', 'scope': 'spec',
                         'token': 'eradicate', 'kind': kind, 'spell_id': None,
                         'trait_id': None, 'candidates': [], 'reason': reason,
                         'source': source, 'options': [], 'aliases': []})
                    for kind, reason, source in (
                        ('action', None, 'runtime_action'),
                        ('buff', 'unbound_buff', 'runtime_expression_resolver'))],
    }


class ReviewedSourceBindingTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'native.json'

    def build(self, payload=None, revision=REVISION, build=BUILD):
        self.path.write_text(json.dumps(payload or runtime_payload()))
        return build_symbol_facts(revision, build, apl_queryset=[], manifest_path=self.path)

    def test_real_eradicate_action_and_buff_have_distinct_source_proven_ids(self):
        result = self.build()
        facts = {r['symbol_kind']: r for r in result.facts if r['token'] == 'eradicate'}
        self.assertEqual(facts['action']['spell_id'], 1225826)
        self.assertEqual(facts['buff']['spell_id'], 1239524)
        self.assertNotEqual(facts['action']['spell_id'], 1226033)  # talent ID is not the action
        self.assertEqual(facts['action']['identity_source'], 'reviewed_simc_source')
        self.assertTrue(facts['action']['identity_reason'].startswith('source_binding:'))
        self.assertEqual(result.completeness, 'runtime/partial')

    def test_real_sync_persists_typed_ids_and_a_second_import_does_not_erase_them(self):
        self.path.write_text(json.dumps(runtime_payload()))
        sync_symbols(REVISION, BUILD, apl_queryset=[], manifest_path=self.path)
        scopes = SimcAplSymbolScope.objects.filter(class_name='demonhunter', spec='devourer',
                                                  symbol__token='eradicate')
        self.assertEqual(dict(scopes.values_list('symbol__symbol_kind', 'spell_id')),
                         {'action': 1225826, 'buff': 1239524})
        again = sync_symbols(REVISION, BUILD, apl_queryset=[], manifest_path=self.path)
        self.assertEqual((again.created, again.updated), (0, 0))

    def test_other_revision_or_game_build_cannot_reuse_reviewed_bindings(self):
        for revision, build in [('f' * 40, BUILD), (REVISION, '12.1.5.99999')]:
            payload = runtime_payload()
            payload.update(simc_revision=revision, game_build=build)
            with self.subTest(revision=revision, build=build):
                result = self.build(payload, revision, build)
                self.assertTrue(all(r.get('spell_id') is None for r in result.facts
                                    if r['token'] == 'eradicate'))

    def test_kind_class_spec_and_hero_scope_are_not_interchangeable(self):
        for changes in [{'kind': 'debuff'}, {'class': 'warlock', 'spec': 'destruction'},
                        {'spec': 'havoc'}, {'scope': 'hero_tree', 'hero_tree': 'void_reaver'}]:
            payload = runtime_payload()
            payload['symbols'] = [dict(payload['symbols'][0], **changes)]
            with self.subTest(changes=changes):
                result = self.build(payload)
                fact = next(r for r in result.facts if r['token'] == 'eradicate')
                self.assertIsNone(fact['spell_id'])

    def test_conflicting_nonempty_runtime_id_or_candidates_are_not_overwritten(self):
        for changes in [{'spell_id': 1226033}, {'candidates': [1225826, 1226033]},
                        {'reason': 'ambiguous_expression_identity'}]:
            payload = runtime_payload()
            payload['symbols'][0].update(changes)
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, 'reviewed source binding conflicts'):
                    self.build(payload)

    def test_source_proven_typed_expression_missing_from_export_survives_resync(self):
        from botend.models import SimcApl
        SimcApl.objects.create(name='Balance', class_name='druid', spec='druid_balance',
                              source='simc_upstream', is_system=True, is_active=True,
                              sync_version=REVISION,
                              content='actions=/moonfire,if=buff.orbit_breaker.up')
        self.path.write_text(json.dumps(runtime_payload()))
        sync_symbols(REVISION, BUILD, manifest_path=self.path)
        scope = SimcAplSymbolScope.objects.get(class_key='druid', spec_key='balance',
                                               symbol__token='orbit_breaker', symbol__symbol_kind='buff')
        self.assertEqual(scope.spell_id, 1303480)
        sync_symbols(REVISION, BUILD, manifest_path=self.path)
        scope.refresh_from_db()
        self.assertTrue(scope.is_active)
        self.assertEqual(scope.spell_id, 1303480)
        self.assertFalse(SimcAplSymbolScope.objects.filter(symbol__token='gory_fur_maul').exists())

    def test_source_proven_missing_talent_uses_real_traitnode_not_spell_or_entry(self):
        from botend.models import SimcApl
        SimcApl.objects.create(name='Destruction', class_name='warlock', spec='warlock_destruction',
                              source='simc_upstream', is_system=True, is_active=True,
                              sync_version=REVISION,
                              content='actions=/conflagrate,if=talent.wither.enabled')
        self.path.write_text(json.dumps(runtime_payload()))
        sync_symbols(REVISION, BUILD, manifest_path=self.path)
        row = SimcAplSymbolScope.objects.get(class_key='warlock', spec_key='destruction',
                                             symbol__token='wither', symbol__symbol_kind='talent')
        self.assertEqual((row.trait_id, row.spell_id), (94840, 445465))
        self.assertNotEqual(row.trait_id, 117437)

    def test_dropped_ambiguous_talent_diagnostic_cannot_be_resurrected_from_seed(self):
        from botend.models import SimcApl
        SimcApl.objects.create(name='Destruction', class_name='warlock', spec='warlock_destruction',
                              source='simc_upstream', is_system=True, is_active=True,
                              sync_version=REVISION,
                              content='actions=/conflagrate,if=talent.wither.enabled')
        payload = runtime_payload()
        payload['symbols'].append({'class': 'warlock', 'spec': 'destruction', 'scope': 'spec',
            'kind': 'talent', 'token': 'wither', 'trait_id': None, 'spell_id': None,
            'candidates': [445465, 445468], 'reason': 'ambiguous_expression_identity',
            'source': 'runtime_expression_resolver', 'aliases': [], 'options': []})
        self.path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, 'reviewed source binding conflicts'):
            sync_symbols(REVISION, BUILD, manifest_path=self.path)

    def test_reviewed_foreground_background_collision_resolves_factory_action_only(self):
        payload = runtime_payload()
        payload['symbols'].append({'class':'monk','spec':'windwalker','scope':'spec',
            'kind':'action','token':'touch_of_karma','spell_id':None,'trait_id':None,
            'candidates':[122470,124280],'reason':'ambiguous_spell_identity',
            'source':'runtime_action','aliases':[],'options':[]})
        self.path.write_text(json.dumps(payload))
        sync_symbols(REVISION, BUILD, manifest_path=self.path)
        row=SimcAplSymbolScope.objects.get(class_key='monk',spec_key='windwalker',
                                          symbol__token='touch_of_karma',symbol__symbol_kind='action')
        self.assertEqual(row.spell_id,122470)
        self.assertEqual(row.identity_candidates,[122470])
        self.assertEqual(row.identity_source,'reviewed_simc_source')

    def test_parent_child_proof_does_not_admit_unknown_candidate_or_replace_positive_identity(self):
        for patch in ({'candidates':[122470,124280,125174]}, {'spell_id':124280},
                      {'reason':'ambiguous_expression_identity'}, {'source':'runtime_expression_resolver'}):
            with self.subTest(patch=patch):
                payload=runtime_payload()
                row={'class':'monk','spec':'windwalker','scope':'spec','kind':'action',
                    'token':'touch_of_karma','spell_id':None,'trait_id':None,
                    'candidates':[122470,124280],'reason':'ambiguous_spell_identity',
                    'source':'runtime_action','aliases':[],'options':[]}
                row.update(patch);payload['symbols'].append(row)
                self.path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ValueError,'reviewed source binding conflicts'):
                    sync_symbols(REVISION,BUILD,manifest_path=self.path)

    def test_duplicate_unresolved_talent_diagnostics_reject_both_orders(self):
        from botend.services.simc_apl.symbol_sync import load_runtime_manifest
        ambiguous={'class':'warlock','spec':'destruction','scope':'spec','kind':'talent',
            'token':'wither','trait_id':None,'spell_id':None,'candidates':[445465,445468],
            'reason':'ambiguous_expression_identity','source':'runtime_expression_resolver',
            'aliases':[],'options':[]}
        unbound=dict(ambiguous,candidates=[],reason='talent_not_found')
        for rows in ([ambiguous,unbound],[unbound,ambiguous]):
            with self.subTest(rows=rows):
                payload=runtime_payload();payload['symbols'].extend(rows)
                self.path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ValueError,'duplicate unresolved identity'):
                    load_runtime_manifest(self.path,REVISION,BUILD)

    def test_valid_native_talent_cannot_hide_an_ambiguous_diagnostic(self):
        payload=runtime_payload()
        ambiguous={'class':'warlock','spec':'destruction','scope':'spec','kind':'talent',
            'token':'wither','trait_id':None,'spell_id':None,'candidates':[445465,445468],
            'reason':'ambiguous_expression_identity','source':'runtime_expression_resolver',
            'aliases':[],'options':[]}
        native=dict(ambiguous,trait_id=94840,spell_id=445465,candidates=[445465],reason=None)
        payload['symbols'].extend([ambiguous,native]);self.path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError,'reviewed source binding conflicts'):
            sync_symbols(REVISION,BUILD,manifest_path=self.path)

    def test_verified_nonempty_runtime_identity_keeps_native_provenance(self):
        payload = runtime_payload()
        payload['symbols'][0]['spell_id'] = 1225826
        fact = next(r for r in self.build(payload).facts
                    if r['symbol_kind'] == 'action' and r['token'] == 'eradicate')
        self.assertEqual(fact['identity_source'], 'runtime_action')
