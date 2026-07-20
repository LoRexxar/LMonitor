from django.db import IntegrityError, transaction
from django.db.models import Index
from django.test import TestCase

from botend.models import SimcAplSymbol


class SimcAplSymbolSchemaTests(TestCase):
    def test_fact_defaults_and_choices(self):
        symbol = SimcAplSymbol.objects.create(
            simc_revision="abc123",
            wow_build="12.0.1.70000",
            token="bloodthirst",
            symbol_kind=SimcAplSymbol.KIND_ACTION,
        )

        self.assertIsNone(symbol.class_name)
        self.assertIsNone(symbol.spec)
        self.assertIsNone(symbol.hero_tree)
        self.assertIsNone(symbol.spell_id)
        self.assertEqual(symbol.source, SimcAplSymbol.SOURCE_MANIFEST)
        self.assertEqual(symbol.aliases, [])
        self.assertEqual(symbol.options, {})
        self.assertTrue(symbol.is_active)
        self.assertIn(
            SimcAplSymbol.KIND_ACTION,
            dict(SimcAplSymbol.SYMBOL_KIND_CHOICES),
        )
        self.assertIn(
            SimcAplSymbol.SOURCE_MANIFEST,
            dict(SimcAplSymbol.SOURCE_CHOICES),
        )
        expected_kinds = {
            "action", "pseudo_action", "action_option", "expression",
            "namespace", "resource", "buff", "debuff", "dot", "cooldown",
            "talent", "hero_tree", "option",
        }
        self.assertTrue(expected_kinds <= set(dict(SimcAplSymbol.SYMBOL_KIND_CHOICES)))
        self.assertTrue(
            {"simc_manifest", "system_apl", "wago", "manual"}
            <= set(dict(SimcAplSymbol.SOURCE_CHOICES))
        )

    def test_json_defaults_are_not_shared(self):
        first = SimcAplSymbol(simc_revision="r1", wow_build="b1", token="one")
        second = SimcAplSymbol(simc_revision="r1", wow_build="b1", token="two")
        first.aliases.append("alias")
        first.options["if"] = {"type": "expression"}

        self.assertEqual(second.aliases, [])
        self.assertEqual(second.options, {})

    def test_spell_id_is_an_integer_fact_not_a_foreign_key(self):
        field = SimcAplSymbol._meta.get_field("spell_id")
        self.assertIsNone(field.remote_field)
        self.assertTrue(field.null)

    def test_query_indexes_and_ordering_are_declared(self):
        indexes = {
            tuple(index.fields)
            for index in SimcAplSymbol._meta.indexes
            if isinstance(index, Index)
        }
        self.assertIn(("simc_revision", "spec", "symbol_kind", "token"), indexes)
        self.assertIn(("simc_revision", "spell_id"), indexes)
        self.assertEqual(
            SimcAplSymbol._meta.ordering,
            ["simc_revision", "symbol_kind", "token", "id"],
        )


class SimcAplSymbolVersioningTests(TestCase):
    def make_symbol(self, **overrides):
        values = {
            "simc_revision": "revision-one",
            "wow_build": "12.0.1.70000",
            "class_name": "warrior",
            "spec": "fury",
            "hero_tree": "slayer",
            "token": "bloodthirst",
            "symbol_kind": SimcAplSymbol.KIND_ACTION,
            "source": SimcAplSymbol.SOURCE_MANIFEST,
        }
        values.update(overrides)
        return SimcAplSymbol.objects.create(**values)

    def test_same_token_can_coexist_across_version_build_spec_and_kind(self):
        self.make_symbol()
        self.make_symbol(simc_revision="revision-two")
        self.make_symbol(wow_build="12.0.1.70001")
        self.make_symbol(spec="arms")
        self.make_symbol(symbol_kind=SimcAplSymbol.KIND_EXPRESSION)

        self.assertEqual(SimcAplSymbol.objects.count(), 5)

    def test_exact_versioned_scope_duplicate_is_rejected(self):
        self.make_symbol()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_symbol()

    def test_global_duplicate_is_rejected_even_with_null_scope(self):
        self.make_symbol(class_name=None, spec=None, hero_tree=None)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_symbol(class_name=None, spec=None, hero_tree=None)

    def test_global_class_spec_and_hero_scopes_are_distinct_facts(self):
        global_symbol = self.make_symbol(class_name=None, spec=None, hero_tree=None)
        class_symbol = self.make_symbol(spec=None, hero_tree=None)
        spec_symbol = self.make_symbol(hero_tree=None)
        hero_symbol = self.make_symbol()

        self.assertIsNone(global_symbol.class_name)
        self.assertEqual(class_symbol.class_name, "warrior")
        self.assertEqual(spec_symbol.spec, "fury")
        self.assertEqual(hero_symbol.hero_tree, "slayer")
        self.assertEqual(SimcAplSymbol.objects.count(), 4)

    def test_scope_normalization_and_canonical_keys(self):
        symbol = self.make_symbol(class_name=" warrior ", spec="  ", hero_tree="")
        symbol.refresh_from_db()
        self.assertEqual((symbol.class_name, symbol.class_key), ("warrior", "warrior"))
        self.assertEqual((symbol.spec, symbol.spec_key), (None, ""))
        self.assertEqual((symbol.hero_tree, symbol.hero_tree_key), (None, ""))

    def test_prepare_normalizes_before_bulk_create(self):
        symbol = SimcAplSymbol(
            simc_revision="bulk", wow_build="build", token="Execute",
            class_name=" warrior ", spec=" ", hero_tree=None,
        )
        SimcAplSymbol.objects.bulk_create([SimcAplSymbol.prepare(symbol)])
        symbol.refresh_from_db()
        self.assertEqual((symbol.class_name, symbol.class_key), ("warrior", "warrior"))
        self.assertEqual((symbol.spec, symbol.spec_key), (None, ""))
        self.assertEqual(symbol.token, "execute")

    def test_database_checks_reject_scope_key_drift_from_direct_update(self):
        symbol = self.make_symbol()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcAplSymbol.objects.filter(pk=symbol.pk).update(class_key="mage")

    def test_database_checks_reject_null_scope_with_drift_key_on_direct_create(self):
        for scope, key in (
            ("class_name", "class_key"),
            ("spec", "spec_key"),
            ("hero_tree", "hero_tree_key"),
        ):
            with self.subTest(scope=scope):
                symbol = SimcAplSymbol(
                    simc_revision=f"create-{scope}", wow_build="build",
                    token="execute", **{scope: None, key: "drift"},
                )
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        SimcAplSymbol.objects.bulk_create([symbol])

    def test_database_checks_reject_null_scope_with_drift_key_on_direct_update(self):
        for scope, key in (
            ("class_name", "class_key"),
            ("spec", "spec_key"),
            ("hero_tree", "hero_tree_key"),
        ):
            with self.subTest(scope=scope):
                symbol = self.make_symbol(token=f"update-{scope}")
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        SimcAplSymbol.objects.filter(pk=symbol.pk).update(
                            **{scope: None, key: "drift"}
                        )

    def test_save_update_fields_persists_scope_prepare_changes(self):
        symbol = self.make_symbol()
        symbol.class_name = " mage "
        symbol.save(update_fields={"class_name"})
        symbol.refresh_from_db()
        self.assertEqual((symbol.class_name, symbol.class_key), ("mage", "mage"))

        symbol.class_name = "  "
        symbol.save(update_fields={"class_name"})
        symbol.refresh_from_db()
        self.assertEqual((symbol.class_name, symbol.class_key), (None, ""))

    def test_save_update_fields_persists_token_canonicalization(self):
        symbol = self.make_symbol()
        symbol.token = "  Execute  "
        symbol.save(update_fields={"token"})
        symbol.refresh_from_db()
        self.assertEqual(symbol.token, "execute")

    def test_inactive_identity_is_reactivated_in_place(self):
        symbol = self.make_symbol(is_active=False)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_symbol(is_active=True)
        SimcAplSymbol.sync_revision_catalog(
            "revision-one", "12.0.1.70000",
            [{
                "class_name": "warrior", "spec": "fury", "hero_tree": "slayer",
                "token": "bloodthirst", "symbol_kind": SimcAplSymbol.KIND_ACTION,
                "source": SimcAplSymbol.SOURCE_SIMC_MANIFEST,
            }],
        )
        symbol.refresh_from_db()
        self.assertTrue(symbol.is_active)
        self.assertEqual(SimcAplSymbol.objects.count(), 1)

    def test_sync_marks_missing_revision_identity_inactive(self):
        missing = self.make_symbol(token="execute")
        self.make_symbol(token="rampage")
        SimcAplSymbol.sync_revision_catalog(
            "revision-one", "12.0.1.70000",
            [{
                "class_name": "warrior", "spec": "fury", "hero_tree": "slayer",
                "token": "rampage", "symbol_kind": SimcAplSymbol.KIND_ACTION,
                "source": SimcAplSymbol.SOURCE_SIMC_MANIFEST,
            }],
        )
        missing.refresh_from_db()
        self.assertFalse(missing.is_active)

    def test_sync_deduplicates_identical_canonical_identity_payloads(self):
        facts = [{
            "class_name": " warrior ", "spec": "fury", "hero_tree": "slayer",
            "token": " BloodThirst ", "symbol_kind": SimcAplSymbol.KIND_ACTION,
            "spell_id": 23881, "source": SimcAplSymbol.SOURCE_SIMC_MANIFEST,
            "aliases": ["bt", "blood_thirst"], "options": {"if": "ready"},
            "is_active": False,
        }, {
            "class_name": "warrior", "spec": "fury", "hero_tree": "slayer",
            "token": "bloodthirst", "symbol_kind": SimcAplSymbol.KIND_ACTION,
            "spell_id": 23881, "source": SimcAplSymbol.SOURCE_SIMC_MANIFEST,
            "aliases": ["bt", "blood_thirst"], "options": {"if": "ready"},
            "is_active": True,
        }]
        SimcAplSymbol.sync_revision_catalog("revision-one", "12.0.1.70000", facts)

        symbol = SimcAplSymbol.objects.get()
        self.assertEqual(symbol.token, "bloodthirst")
        self.assertTrue(symbol.is_active)

    def test_sync_rejects_conflicting_duplicate_before_writes_or_deactivation(self):
        existing = self.make_symbol(token="existing")
        facts = [{
            "class_name": "warrior", "spec": "fury", "hero_tree": "slayer",
            "token": "execute", "symbol_kind": SimcAplSymbol.KIND_ACTION,
            "spell_id": 5308, "aliases": ["execute"],
        }, {
            "class_name": " warrior ", "spec": "fury", "hero_tree": "slayer",
            "token": " EXECUTE ", "symbol_kind": SimcAplSymbol.KIND_ACTION,
            "spell_id": 5308, "aliases": ["execute", "exec"],
        }]

        with self.assertRaisesRegex(ValueError, "conflicting duplicate identity"):
            SimcAplSymbol.sync_revision_catalog(
                "revision-one", "12.0.1.70000", reversed(facts)
            )

        existing.refresh_from_db()
        self.assertTrue(existing.is_active)
        self.assertEqual(SimcAplSymbol.objects.count(), 1)

    def test_token_identity_is_case_insensitive(self):
        self.make_symbol(token="BloodThirst")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_symbol(token="bloodthirst")
