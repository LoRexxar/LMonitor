import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from botend.models import SimcApl, SimcAplSymbol
from botend.services.simc_apl.symbol_sync import build_symbol_facts, sync_symbols


class SimcAplSymbolSyncTests(TestCase):
    def setUp(self):
        SimcApl.objects.create(name='Fury', class_name='warrior', spec='warrior_fury',
            content='actions=/bloodthirst,if=buff.enrage.up\nactions+=/rampage',
            source='simc_upstream', is_system=True, sync_version='sha1')

    def test_observed_actions_and_namespaces_come_from_ast(self):
        result = build_symbol_facts('sha1', 'b1')
        facts = {(f['token'], f['symbol_kind']) for f in result.facts}
        self.assertIn(('bloodthirst', 'action'), facts)
        self.assertIn(('rampage', 'action'), facts)
        self.assertIn(('buff', 'namespace'), facts)
        self.assertIn(('if', 'action_option'), facts)
        self.assertIn(('buff.enrage.up', 'expression'), facts)
        self.assertEqual(result.completeness, 'observed/partial')

    def test_audited_engine_symbols_are_global_not_copied_per_spec(self):
        result = build_symbol_facts('sha1', 'b1')
        wait = next(f for f in result.facts
                    if f['token'] == 'wait' and f['symbol_kind'] == 'pseudo_action')
        condition = next(f for f in result.facts
                         if f['token'] == 'if' and f['symbol_kind'] == 'option')
        self.assertEqual((wait['class_name'], wait['spec']), (None, None))
        self.assertEqual((condition['class_name'], condition['spec']), (None, None))
        self.assertEqual(sum(f['token'] == 'wait' for f in result.facts), 1)

    def test_repeat_summary_and_missing_deactivation(self):
        first = sync_symbols('sha1', 'b1')
        initial_count = SimcAplSymbol.objects.filter(
            simc_revision='sha1', wow_build='b1', is_active=True).count()
        self.assertEqual(first.created, initial_count)
        second = sync_symbols('sha1', 'b1')
        self.assertEqual((second.created, second.updated, second.unchanged),
                         (0, 0, initial_count))
        SimcApl.objects.get().delete()
        third = sync_symbols('sha1', 'b1')
        self.assertEqual(third.deactivated, 5)

    def test_build_or_validation_failure_does_not_deactivate(self):
        SimcAplSymbol.objects.create(simc_revision='sha1', wow_build='b1', token='old')
        with mock.patch('botend.services.simc_apl.symbol_sync.build_symbol_facts',
                        side_effect=ValueError('bad facts')):
            with self.assertRaisesRegex(ValueError, 'bad facts'):
                sync_symbols('sha1', 'b1')
        self.assertTrue(SimcAplSymbol.objects.get().is_active)

    @override_settings(SIMC_APL_SYMBOL_BINDINGS=[
        {'token': 'bloodthirst', 'symbol_kind': 'action', 'spell_id': 23881,
         'class_name': 'warrior', 'spec': 'fury', 'hero_tree': None}])
    def test_only_explicit_manual_token_spell_binding_is_accepted(self):
        result = build_symbol_facts('sha1', 'b1')
        bloodthirst = next(f for f in result.facts if f['token'] == 'bloodthirst' and
                           f['symbol_kind'] == 'action')
        self.assertEqual(bloodthirst['spell_id'], 23881)

    @override_settings(SIMC_APL_SYMBOL_BINDINGS=[{'spell_id': 23881, 'name': 'Bloodthirst'}])
    def test_name_only_binding_is_invalid_not_guessed(self):
        result = build_symbol_facts('sha1', 'b1')
        self.assertEqual(result.invalid, 1)
        self.assertTrue(all(f.get('spell_id') != 23881 for f in result.facts))

    @override_settings(SIMC_APL_SYMBOL_BINDINGS=['not-a-mapping'])
    def test_malformed_binding_is_counted_invalid(self):
        self.assertEqual(build_symbol_facts('sha1', 'b1').invalid, 1)

    def test_binding_requires_kind_and_exact_complete_scope(self):
        missing_kind = [{'token': 'bloodthirst', 'spell_id': 23881,
                         'class_name': 'warrior', 'spec': 'fury', 'hero_tree': None}]
        broad_scope = [{'token': 'bloodthirst', 'symbol_kind': 'action', 'spell_id': 23881,
                        'class_name': 'warrior', 'spec': None, 'hero_tree': None}]
        self.assertEqual(build_symbol_facts('sha1', 'b1', bindings=missing_kind).invalid, 1)
        self.assertEqual(build_symbol_facts('sha1', 'b1', bindings=broad_scope).invalid, 1)

    def test_invalid_binding_blocks_non_dry_run_before_any_catalog_write(self):
        old = SimcAplSymbol.objects.create(
            simc_revision='sha1', wow_build='b1', token='old', symbol_kind='action')
        invalid = [{'token': 'bloodthirst', 'spell_id': 23881,
                    'class_name': 'warrior', 'spec': 'fury', 'hero_tree': None}]
        with self.assertRaisesRegex(ValueError, 'invalid'):
            sync_symbols('sha1', 'b1', bindings=invalid)
        old.refresh_from_db()
        self.assertTrue(old.is_active)
        self.assertEqual(SimcAplSymbol.objects.count(), 1)

    def test_invalid_binding_dry_run_reports_without_writing(self):
        invalid = [{'token': 'bloodthirst', 'spell_id': 23881}]
        summary = sync_symbols('sha1', 'b1', bindings=invalid, dry_run=True)
        self.assertEqual(summary.invalid, 1)
        self.assertEqual(SimcAplSymbol.objects.count(), 0)

    def test_invalid_expression_forms_block_publish_and_create_no_observed_facts(self):
        apl = SimcApl.objects.get()
        for expression in ('buff..foo.up', 'buff.foo.', '$evil', '=='):
            with self.subTest(expression=expression):
                apl.content = f'actions=/spell,if={expression}'
                apl.save()
                result = build_symbol_facts('sha1', 'b1')
                self.assertGreater(result.invalid, 0)
                self.assertFalse(any(f['source'] == 'system_apl' for f in result.facts))
                summary = sync_symbols('sha1', 'b1', dry_run=True)
                self.assertGreater(summary.invalid, 0)
                self.assertEqual(SimcAplSymbol.objects.count(), 0)
                with self.assertRaisesRegex(ValueError, 'invalid'):
                    sync_symbols('sha1', 'b1')
                self.assertEqual(SimcAplSymbol.objects.count(), 0)

    def test_valid_segmented_identifier_is_observed(self):
        apl = SimcApl.objects.get()
        apl.content = 'actions=/spell,if=buff.foo.up'
        apl.save()
        result = build_symbol_facts('sha1', 'b1')
        self.assertEqual(result.invalid, 0)
        self.assertTrue(any(f['token'] == 'buff.foo.up' for f in result.facts))
