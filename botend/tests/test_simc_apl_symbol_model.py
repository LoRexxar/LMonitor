from django.db import IntegrityError, transaction
from django.db.models import Index
from django.test import TestCase

from botend.models import SimcAplSymbol, SimcAplSymbolScope


class SimcAplSymbolSchemaTests(TestCase):
    def test_field_identity_contains_only_token_and_kind(self):
        symbol = SimcAplSymbol.objects.create(
            token=' BloodThirst ', symbol_kind=SimcAplSymbol.KIND_ACTION,
        )

        self.assertEqual(symbol.token, 'bloodthirst')
        self.assertTrue(symbol.is_active)
        self.assertEqual(
            SimcAplSymbol._meta.ordering,
            ['symbol_kind', 'token', 'id'],
        )
        constraint = next(
            value for value in SimcAplSymbol._meta.constraints
            if value.name == 'simc_symbol_token_kind_uniq'
        )
        self.assertEqual(tuple(constraint.fields), ('token', 'symbol_kind'))
        self.assertNotIn('simc_revision', {field.name for field in SimcAplSymbol._meta.fields})
        self.assertNotIn('wow_build', {field.name for field in SimcAplSymbol._meta.fields})

    def test_scope_defaults_and_choices(self):
        symbol = SimcAplSymbol.objects.create(token='bloodthirst', symbol_kind='action')
        scope = SimcAplSymbolScope.objects.create(symbol=symbol)

        self.assertIsNone(scope.class_name)
        self.assertIsNone(scope.spec)
        self.assertIsNone(scope.hero_tree)
        self.assertIsNone(scope.spell_id)
        self.assertEqual(scope.source, SimcAplSymbol.SOURCE_MANIFEST)
        self.assertEqual(scope.aliases, [])
        self.assertEqual(scope.options, {})
        self.assertEqual(scope.name_en, '')
        self.assertEqual(scope.name_zh, '')
        self.assertEqual(scope.metadata, {})
        self.assertTrue(scope.is_active)
        self.assertIn(SimcAplSymbol.KIND_ACTION, dict(SimcAplSymbol.SYMBOL_KIND_CHOICES))
        self.assertIn(SimcAplSymbol.SOURCE_MANIFEST, dict(SimcAplSymbol.SOURCE_CHOICES))

    def test_scope_spell_id_is_integer_not_foreign_key(self):
        field = SimcAplSymbolScope._meta.get_field('spell_id')
        self.assertIsNone(field.remote_field)
        self.assertTrue(field.null)

    def test_query_indexes_are_declared_on_subject_and_scope(self):
        symbol_indexes = {
            tuple(index.fields) for index in SimcAplSymbol._meta.indexes
            if isinstance(index, Index)
        }
        scope_indexes = {
            tuple(index.fields) for index in SimcAplSymbolScope._meta.indexes
            if isinstance(index, Index)
        }
        self.assertIn(('symbol_kind', 'token'), symbol_indexes)
        self.assertIn(('class_name', 'spec', 'hero_tree', 'is_active'), scope_indexes)
        self.assertIn(('spell_id',), scope_indexes)


class SimcAplSymbolIdentityTests(TestCase):
    def make_scope(self, *, token='bloodthirst', kind='action', **values):
        symbol, _created = SimcAplSymbol.objects.get_or_create(
            token=token, symbol_kind=kind,
        )
        return SimcAplSymbolScope.objects.create(symbol=symbol, **values)

    def test_same_token_kind_is_one_subject_with_multiple_scopes(self):
        self.make_scope(class_name='warrior', spec='fury', spell_id=23881)
        self.make_scope(class_name='warrior', spec='arms', spell_id=None)
        self.make_scope(class_name='mage', spec='fire', spell_id=123)
        self.make_scope(token='bloodthirst', kind='buff', class_name='warrior')

        self.assertEqual(SimcAplSymbol.objects.count(), 2)
        self.assertEqual(SimcAplSymbolScope.objects.count(), 4)

    def test_duplicate_token_kind_subject_is_rejected(self):
        SimcAplSymbol.objects.create(token='execute', symbol_kind='action')
        with self.assertRaises(IntegrityError), transaction.atomic():
            SimcAplSymbol.objects.create(token='execute', symbol_kind='action')

    def test_duplicate_scope_binding_is_rejected(self):
        self.make_scope(token='execute', class_name='warrior', spec='fury')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_scope(token='execute', class_name='warrior', spec='fury')

    def test_scope_normalization_and_canonical_keys(self):
        scope = self.make_scope(class_name=' Warrior ', spec='  ', hero_tree='')
        scope.refresh_from_db()
        self.assertEqual((scope.class_name, scope.class_key), ('warrior', 'warrior'))
        self.assertEqual((scope.spec, scope.spec_key), (None, ''))
        self.assertEqual((scope.hero_tree, scope.hero_tree_key), (None, ''))

    def test_scope_checks_reject_key_drift(self):
        scope = self.make_scope(class_name='warrior', spec='fury')
        with self.assertRaises(IntegrityError), transaction.atomic():
            SimcAplSymbolScope.objects.filter(pk=scope.pk).update(class_key='mage')

    def test_save_update_fields_persists_scope_prepare_changes(self):
        scope = self.make_scope(class_name='warrior', spec='fury')
        scope.class_name = ' Mage '
        scope.save(update_fields={'class_name'})
        scope.refresh_from_db()
        self.assertEqual((scope.class_name, scope.class_key), ('mage', 'mage'))

    def test_sync_different_build_updates_same_physical_rows(self):
        fact = {
            'class_name': 'warrior', 'spec': 'fury', 'hero_tree': None,
            'token': 'bloodthirst', 'symbol_kind': 'action', 'spell_id': 23881,
            'source': SimcAplSymbol.SOURCE_SIMC_MANIFEST,
        }
        SimcAplSymbol.sync_revision_catalog('revision-one', 'build-one', [fact])
        SimcAplSymbol.sync_revision_catalog('revision-two', 'build-two', [fact])

        self.assertEqual(SimcAplSymbol.objects.count(), 1)
        self.assertEqual(SimcAplSymbolScope.objects.count(), 1)
        self.assertTrue(SimcAplSymbolScope.objects.get().is_active)

    def test_sync_scope_correction_does_not_create_duplicate_subject(self):
        first = {
            'class_name': 'warrior', 'spec': None, 'hero_tree': None,
            'token': 'burst_of_power', 'symbol_kind': 'buff',
            'source': SimcAplSymbol.SOURCE_SIMC_MANIFEST,
        }
        corrected = dict(first, spec='fury')
        SimcAplSymbol.sync_revision_catalog('revision-one', 'build-one', [first])
        SimcAplSymbol.sync_revision_catalog('revision-two', 'build-two', [corrected])

        self.assertEqual(SimcAplSymbol.objects.count(), 1)
        self.assertEqual(SimcAplSymbolScope.objects.filter(is_active=True).count(), 1)
        self.assertEqual(SimcAplSymbolScope.objects.get(is_active=True).spec, 'fury')

    def test_runtime_sync_preserves_packaged_localization(self):
        scope = self.make_scope(
            class_name='warrior', spec='fury', name_en='Bloodthirst', name_zh='嗜血',
            localization_source='wowhead', metadata={'covered_specs': ['fury']},
        )
        SimcAplSymbol.sync_revision_catalog('revision-two', 'build-two', [{
            'class_name': 'warrior', 'spec': 'fury', 'hero_tree': None,
            'token': 'bloodthirst', 'symbol_kind': 'action', 'spell_id': 23881,
            'source': SimcAplSymbol.SOURCE_SIMC_MANIFEST,
        }])

        scope.refresh_from_db()
        self.assertEqual((scope.name_en, scope.name_zh), ('Bloodthirst', '嗜血'))
        self.assertEqual(scope.metadata, {'covered_specs': ['fury']})

    def test_sync_rejects_conflicting_canonical_duplicate_before_writes(self):
        self.make_scope(token='existing')
        facts = [{
            'class_name': 'warrior', 'spec': 'fury', 'hero_tree': None,
            'token': 'execute', 'symbol_kind': 'action', 'spell_id': 5308,
            'aliases': ['execute'],
        }, {
            'class_name': ' Warrior ', 'spec': 'fury', 'hero_tree': None,
            'token': ' EXECUTE ', 'symbol_kind': 'action', 'spell_id': 5308,
            'aliases': ['execute', 'exec'],
        }]

        with self.assertRaisesRegex(ValueError, 'conflicting duplicate identity'):
            SimcAplSymbol.sync_revision_catalog('revision', 'build', facts)
        self.assertEqual(SimcAplSymbol.objects.count(), 1)
        self.assertTrue(SimcAplSymbolScope.objects.get().is_active)

    def test_token_identity_is_case_insensitive(self):
        SimcAplSymbol.objects.create(token='BloodThirst', symbol_kind='action')
        with self.assertRaises(IntegrityError), transaction.atomic():
            SimcAplSymbol.objects.create(token='bloodthirst', symbol_kind='action')
