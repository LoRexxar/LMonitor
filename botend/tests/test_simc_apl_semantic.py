from django.test import SimpleTestCase

from botend.services.simc_apl import analyze, parse
from botend.services.simc_apl.ast import SourcePosition, SourceRange


class SimcAplSemanticTests(SimpleTestCase):
    def analyze(self, source):
        return analyze(parse(source))

    def test_collects_main_named_lists_actions_variables_and_references(self):
        result = self.analyze(
            "actions=/variable,name=pool,value=1/call_action_list,name=burst,if=variable.pool\n"
            "actions.burst+=/spell\n"
        )

        self.assertEqual(set(result.symbols.action_lists), {None, "burst"})
        self.assertEqual(set(result.symbols.variables), {"pool"})
        self.assertEqual([site.name for site in result.symbols.list_references], ["burst"])
        self.assertEqual([site.name for site in result.symbols.variable_references], ["pool"])
        self.assertEqual(result.diagnostics, ())

    def test_named_default_is_ignored_and_does_not_alias_the_main_list(self):
        result = self.analyze(
            "actions=/one\n"
            "actions.default=/two\n"
            "actions+=/call_action_list,name=default\n"
        )
        codes = [d.code for d in result.diagnostics]
        self.assertIn("ignored-default-action-list", codes)
        self.assertIn("undefined-action-list", codes)
        self.assertEqual(set(result.symbols.action_lists), {None})

    def test_named_default_assignment_never_resets_the_main_list(self):
        result = self.analyze(
            "actions+=/variable,name=live,value=1\n"
            "actions.default=/variable,name=ignored,value=1\n"
            "actions+=/spell,if=variable.live"
        )
        codes = [d.code for d in result.diagnostics]
        self.assertNotIn("action-list-reset", codes)
        self.assertNotIn("undefined-variable", codes)
        self.assertNotIn("ignored", result.symbols.variables)

    def test_missing_list_is_error_at_exact_absolute_name_value_range(self):
        result = self.analyze("\n  actions+=/call_action_list,name=missing,if=1")
        diagnostic = next(d for d in result.diagnostics if d.code == "undefined-action-list")
        self.assertEqual(diagnostic.severity, "error")
        self.assertEqual(diagnostic.range, SourceRange(
            SourcePosition(2, 35), SourcePosition(2, 42)))

    def test_multiple_appends_are_not_duplicate_but_later_reset_warns(self):
        normal = self.analyze("actions.foo+=/one\nactions.foo+=/two")
        reset = self.analyze("actions.foo+=/one\nactions.foo=/two")
        self.assertNotIn("action-list-reset", [d.code for d in normal.diagnostics])
        warning = next(d for d in reset.diagnostics if d.code == "action-list-reset")
        self.assertEqual(warning.severity, "warning")
        self.assertEqual(warning.range, SourceRange(
            SourcePosition(2, 9), SourcePosition(2, 12)))

    def test_unreferenced_named_list_is_info_but_engine_entry_lists_are_not(self):
        result = self.analyze(
            "actions=/one\nactions.precombat=/snapshot_stats\nactions.orphan=/two"
        )
        diagnostic = next(
            d for d in result.diagnostics if d.code == "unreferenced-action-list")
        self.assertEqual(diagnostic.severity, "info")
        self.assertEqual(diagnostic.range, SourceRange(
            SourcePosition(3, 9), SourcePosition(3, 15)))

    def test_variable_forward_definition_is_known_and_repeated_updates_are_legal(self):
        result = self.analyze(
            "actions=/spell,if=variable.pool\n"
            "actions+=/variable,name=pool,value=1\n"
            "actions+=/variable,name=pool,op=add,value=1\n"
        )
        self.assertNotIn("undefined-variable", [d.code for d in result.diagnostics])
        self.assertNotIn("duplicate-variable", [d.code for d in result.diagnostics])

    def test_variable_names_are_case_insensitive_but_keep_source_spelling(self):
        result = self.analyze(
            "actions=/variable,name=Pool,value=1\n"
            "actions+=/variable,name=pOOL,op=add,value=1\n"
            "actions+=/spell,if=variable.pool"
        )

        self.assertEqual(set(result.symbols.variables), {"pool"})
        self.assertEqual(
            [definition.name for definition in result.symbols.variables["pool"]],
            ["Pool", "pOOL"],
        )
        self.assertNotIn("undefined-variable", [d.code for d in result.diagnostics])

    def test_reset_discards_prior_missing_edges_cycles_and_variable_definitions(self):
        missing = self.analyze(
            "actions=/call_action_list,name=missing\n"
            "actions=/spell"
        )
        cycle = self.analyze(
            "actions.a=/call_action_list,name=b\n"
            "actions.b=/call_action_list,name=a\n"
            "actions.a=/spell"
        )
        variable = self.analyze(
            "actions=/variable,name=gone,value=1\n"
            "actions=/spell,if=variable.gone"
        )

        self.assertNotIn("undefined-action-list", [d.code for d in missing.diagnostics])
        self.assertNotIn("recursive-action-list", [d.code for d in cycle.diagnostics])
        self.assertIn("undefined-variable", [d.code for d in variable.diagnostics])
        self.assertNotIn("gone", variable.symbols.variables)

    def test_reset_keeps_subsequent_definitions_and_appends(self):
        result = self.analyze(
            "actions=/variable,name=old,value=1\n"
            "actions=/variable,name=New,value=1\n"
            "actions+=/variable,name=nEW,op=add,value=1\n"
            "actions+=/spell,if=variable.new"
        )
        self.assertEqual(len(result.symbols.variables["new"]), 2)
        self.assertNotIn("undefined-variable", [d.code for d in result.diagnostics])

    def test_non_identifier_action_list_names_resolve_exactly_and_cycle(self):
        result = self.analyze(
            "actions.foo-bar=/call_action_list,name=1phase\n"
            "actions.1phase=/run_action_list,name=foo-bar"
        )
        self.assertEqual([site.name for site in result.symbols.list_references], [
            "1phase", "foo-bar",
        ])
        self.assertEqual(
            [d.code for d in result.diagnostics].count("recursive-action-list"), 2)
        self.assertNotIn("undefined-action-list", [d.code for d in result.diagnostics])

    def test_action_list_reference_names_are_trimmed_and_case_sensitive(self):
        result = self.analyze(
            "actions.Foo=/one\n"
            "actions=/call_action_list,name= Foo /call_action_list,name=foo"
        )
        self.assertEqual([site.name for site in result.symbols.list_references], ["Foo", "foo"])
        undefined = [d for d in result.diagnostics if d.code == "undefined-action-list"]
        self.assertEqual(len(undefined), 1)
        self.assertIn("'foo'", undefined[0].message)

    def test_invalid_action_list_references_are_diagnosed_without_symbol_edges(self):
        invalid_names = (
            ")(", "bad?", "bad;", r"bad\name", "'bad'", '"bad"', "bad.name",
        )

        for name in invalid_names:
            with self.subTest(name=name):
                result = self.analyze(f"actions=/call_action_list,name={name}")
                codes = [diagnostic.code for diagnostic in result.diagnostics]
                self.assertIn("invalid-action-list-name", codes)
                self.assertNotIn("undefined-action-list", codes)
                self.assertEqual(result.symbols.list_references, [])

    def test_upstream_variable_names_may_start_with_digits(self):
        result = self.analyze(
            "actions.precombat=/variable,name=20ssteroid,op=set,value=1\n"
            "actions=/spell,if=variable.20ssteroid"
        )
        self.assertIn("20ssteroid", result.symbols.variables)
        self.assertNotIn("undefined-variable", [d.code for d in result.diagnostics])

    def test_unknown_variable_reference_warns_at_identifier_component(self):
        result = self.analyze("  actions=/spell,if=buff.x.up&variable.unknown>0")
        diagnostic = next(d for d in result.diagnostics if d.code == "undefined-variable")
        self.assertEqual(diagnostic.severity, "warning")
        self.assertEqual(diagnostic.range, SourceRange(
            SourcePosition(1, 40), SourcePosition(1, 47)))

    def test_variable_without_name_and_incomplete_setif_are_diagnosed(self):
        result = self.analyze("actions=/variable,value=1/variable,name=x,op=setif,value=2")
        codes = [d.code for d in result.diagnostics]
        self.assertIn("missing-variable-name", codes)
        self.assertIn("incomplete-variable-setif", codes)

    def test_direct_and_indirect_call_cycles_point_to_reference_sites(self):
        direct = self.analyze("actions.loop=call_action_list,name=loop")
        indirect = self.analyze(
            "actions.a=call_action_list,name=b\n"
            "actions.b=run_action_list,name=a\n"
        )
        self.assertEqual(
            [d.code for d in direct.diagnostics].count("recursive-action-list"), 1)
        cycle = [d for d in indirect.diagnostics if d.code == "recursive-action-list"]
        self.assertEqual(len(cycle), 2)
        self.assertTrue(all(d.severity == "warning" for d in cycle))
        self.assertEqual(cycle[0].range, SourceRange(
            SourcePosition(1, 33), SourcePosition(1, 34)))

    def test_bad_local_nodes_do_not_prevent_collecting_later_valid_nodes(self):
        document = parse(
            "actions=/call_action_list,bad/name,broken/call_action_list,name=missing\n"
            "actions.ok=/variable,name=known,value=1"
        )
        result = analyze(document)
        self.assertIn("ok", result.symbols.action_lists)
        self.assertIn("known", result.symbols.variables)
        self.assertIn("undefined-action-list", [d.code for d in result.diagnostics])

    def test_empty_reference_name_is_not_reported_as_an_undefined_symbol(self):
        result = self.analyze("actions=call_action_list,name=")
        codes = [d.code for d in result.diagnostics]
        self.assertIn("empty-action-list-name", codes)
        self.assertNotIn("undefined-action-list", codes)

    def test_representative_upstream_style_document_has_no_false_positives(self):
        result = self.analyze(
            "actions.precombat+=/snapshot_stats\n"
            "actions=/variable,name=sync,value=talent.foo.enabled\n"
            "actions+=/call_action_list,name=cds,if=variable.sync\n"
            "actions.cds+=/variable,name=sync,op=set,value=0\n"
            "actions.cds+=/spell,target_if=min:debuff.foo.remains\n"
        )
        self.assertEqual(result.diagnostics, ())
