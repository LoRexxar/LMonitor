import json
import tempfile
from pathlib import Path

from django.test import TestCase

from botend.models import SimcApl, SimcAplSymbol
from botend.services.simc_apl.symbol_sync import load_runtime_manifest, sync_symbols


REVISION = 'a' * 40
BUILD = '12.0.5.12345'


def manifest(symbols=None, **overrides):
    payload = {
        'schema_version': 1,
        'simc_revision': REVISION,
        'game_build': BUILD,
        'generated_at': '2026-07-21T00:00:00Z',
        'completeness': {
            'status': 'partial',
            'modules': {'warrior/fury': 'runtime_initialized'},
            'limitations': ['Only actions created by initialized actor APLs are enumerable.'],
        },
        'symbols': symbols or [{
            'class': 'warrior', 'spec': 'fury', 'scope': 'spec',
            'token': 'bloodthirst', 'kind': 'action', 'spell_id': 23881,
            'source': 'runtime_action', 'options': ['if', 'target_if'], 'aliases': [],
        }],
    }
    payload.update(overrides)
    return payload


class RuntimeManifestImportTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / 'apl-metadata.json'

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding='utf-8')
        return str(self.path)

    def test_valid_manifest_maps_runtime_facts_without_using_generated_at_identity(self):
        first = load_runtime_manifest(self.write(manifest()), REVISION, BUILD)
        changed_time = manifest(generated_at='2030-01-01T00:00:00Z')
        second = load_runtime_manifest(self.write(changed_time), REVISION, BUILD)
        self.assertEqual(first.facts, second.facts)
        fact = first.facts[0]
        self.assertEqual(fact['source'], SimcAplSymbol.SOURCE_SIMC_MANIFEST)
        self.assertEqual(fact['spell_id'], 23881)
        self.assertEqual(fact['options'], ['if', 'target_if'])
        self.assertEqual(first.completeness, 'runtime/partial')

    def test_schema_revision_and_build_mismatch_block_import(self):
        cases = [
            (manifest(schema_version=2), 'schema_version'),
            (manifest(simc_revision='b' * 40), 'revision'),
            (manifest(game_build='other'), 'game_build'),
        ]
        for payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                load_runtime_manifest(self.write(payload), REVISION, BUILD)

    def test_malformed_json_and_wrong_field_types_are_rejected(self):
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'JSON'):
            load_runtime_manifest(str(self.path), REVISION, BUILD)
        invalid = manifest(symbols=[{
            'class': 'warrior', 'spec': 'fury', 'scope': 'spec', 'token': 'x',
            'kind': 'action', 'spell_id': True, 'source': 'runtime_action',
            'options': {}, 'aliases': [],
        }])
        with self.assertRaisesRegex(ValueError, 'symbols\[0\]'):
            load_runtime_manifest(self.write(invalid), REVISION, BUILD)

    def test_completeness_cannot_claim_complete_when_limitations_or_failed_modules_exist(self):
        payload = manifest(completeness={
            'status': 'complete', 'modules': {'mage/arcane': 'failed'},
            'limitations': ['dynamic factories are not enumerable'],
        })
        with self.assertRaisesRegex(ValueError, 'completeness'):
            load_runtime_manifest(self.write(payload), REVISION, BUILD)

    def test_runtime_binding_wins_over_observed_payload_but_observed_coverage_remains(self):
        SimcApl.objects.create(
            name='Fury', class_name='warrior', spec='warrior_fury',
            content='actions=/bloodthirst\nactions+=/rampage', source='simc_upstream',
            is_system=True, is_active=True, sync_version=REVISION,
        )
        summary = sync_symbols(REVISION, BUILD, manifest_path=self.write(manifest()))
        self.assertEqual(summary.completeness, 'runtime/partial')
        bloodthirst = SimcAplSymbol.objects.get(
            simc_revision=REVISION, wow_build=BUILD, token='bloodthirst', symbol_kind='action')
        rampage = SimcAplSymbol.objects.get(
            simc_revision=REVISION, wow_build=BUILD, token='rampage', symbol_kind='action')
        self.assertEqual((bloodthirst.source, bloodthirst.spell_id),
                         (SimcAplSymbol.SOURCE_SIMC_MANIFEST, 23881))
        self.assertEqual(rampage.source, SimcAplSymbol.SOURCE_SYSTEM_APL)

    def test_manifest_covers_observed_actions_for_structurally_different_specs(self):
        symbols = []
        apl_cases = [
            ('warrior', 'warrior_fury', ['bloodthirst', 'rampage']),
            ('mage', 'mage_arcane', ['arcane_blast', 'arcane_barrage']),
            ('druid', 'druid_feral', ['rake', 'shred']),
        ]
        for class_name, spec_key, actions in apl_cases:
            spec = spec_key.split('_', 1)[1]
            SimcApl.objects.create(
                name=spec_key, class_name=class_name, spec=spec_key,
                content='\n'.join('actions%s=/%s' % ('' if i == 0 else '+', action)
                                  for i, action in enumerate(actions)),
                source='simc_upstream', is_system=True, is_active=True,
                sync_version=REVISION,
            )
            symbols.extend({
                'class': class_name, 'spec': spec, 'scope': 'spec', 'token': action,
                'kind': 'action', 'spell_id': None, 'source': 'runtime_action',
                'options': [], 'aliases': [],
            } for action in actions)
        runtime = load_runtime_manifest(self.write(manifest(symbols=symbols)), REVISION, BUILD)
        runtime_actions = {(f['class_name'], f['spec'], f['token']) for f in runtime.facts}
        for class_name, spec_key, actions in apl_cases:
            spec = spec_key.split('_', 1)[1]
            self.assertTrue(all((class_name, spec, action) in runtime_actions for action in actions))
