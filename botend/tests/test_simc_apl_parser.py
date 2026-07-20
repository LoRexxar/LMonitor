from django.test import SimpleTestCase

from botend.services.simc_apl import parse
from botend.services.simc_apl.ast import (
    ActionAssignment,
    BlankLine,
    CommentLine,
    InvalidActionEntry,
    InvalidLine,
    InvalidOption,
    SourcePosition,
    SourceRange,
)
from botend.services.simc_apl.lexer import LineKind, lex


class SimcAplParserTests(SimpleTestCase):
    """Source ranges are 1-based and end-exclusive."""

    def test_lexer_preserves_source_lines_and_classifies_top_level_lines(self):
        source = "\n  # keep me\n  actions+=/charge\nnot_an_apl\n"

        tokens = lex(source)

        self.assertEqual([token.kind for token in tokens], [
            LineKind.BLANK, LineKind.COMMENT, LineKind.ACTION, LineKind.INVALID,
        ])
        self.assertEqual([token.text for token in tokens], [
            "", "  # keep me", "  actions+=/charge", "not_an_apl",
        ])
        self.assertEqual(tokens[2].range, SourceRange(
            SourcePosition(3, 1), SourcePosition(3, 19)))

    def test_document_preserves_blank_comment_indent_and_original_source(self):
        source = "\n  # keep me\n  actions=auto_attack\n"

        document = parse(source)

        self.assertEqual(document.source, source)
        self.assertIsInstance(document.lines[0], BlankLine)
        self.assertIsInstance(document.lines[1], CommentLine)
        self.assertEqual(document.lines[1].text, "  # keep me")
        assignment = document.lines[2]
        self.assertIsInstance(assignment, ActionAssignment)
        self.assertEqual(assignment.indent, "  ")
        self.assertEqual(assignment.text, "  actions=auto_attack")
        self.assertEqual(document.range, SourceRange(
            SourcePosition(1, 1), SourcePosition(4, 1)))
        self.assertEqual(document.issues, ())

    def test_parses_assignment_operators_optional_slash_and_named_lists(self):
        source = "actions=auto_attack\nactions+=/bloodthirst\nactions.precombat=snapshot_stats"

        document = parse(source)
        first, second, third = document.lines

        self.assertEqual((first.list_name, first.operator), (None, "="))
        self.assertEqual(first.actions[0].name, "auto_attack")
        self.assertEqual((second.list_name, second.operator), (None, "+="))
        self.assertEqual(second.actions[0].name, "bloodthirst")
        self.assertEqual((third.list_name, third.operator), ("precombat", "="))
        self.assertEqual(third.actions[0].name, "snapshot_stats")

    def test_named_action_lists_accept_upstream_non_identifier_keys(self):
        document = parse(
            "actions.foo-bar=/one\n"
            "actions.1phase+=/two"
        )

        self.assertEqual(
            [line.list_name for line in document.lines], ["foo-bar", "1phase"])
        self.assertEqual(document.issues, ())

    def test_named_action_lists_still_reject_empty_or_structurally_broken_keys(self):
        document = parse(
            "actions.=one\n"
            "actions.bad name=two\n"
            "actions.bad/name=three"
        )

        self.assertTrue(all(isinstance(line, InvalidLine) for line in document.lines))

    def test_named_action_lists_reject_punctuation_outside_the_explicit_whitelist(self):
        invalid_names = (
            ")(", "bad?", "bad;", r"bad\name", "'bad'", '"bad"',
        )

        for name in invalid_names:
            with self.subTest(name=name):
                document = parse(f"actions.{name}=one")
                self.assertIsInstance(document.lines[0], InvalidLine)
                self.assertEqual([issue.code for issue in document.issues], ["invalid-line"])

    def test_parses_multiple_actions_and_options_on_one_line(self):
        document = parse(
            "actions.cooldowns+=/recklessness,if=buff.avatar.up"
            "/avatar,if=rage>=80"
        )

        assignment = document.lines[0]
        self.assertEqual([action.name for action in assignment.actions], [
            "recklessness", "avatar",
        ])
        self.assertEqual(assignment.actions[0].options[0].name, "if")
        self.assertEqual(assignment.actions[0].options[0].value, "buff.avatar.up")
        self.assertEqual(assignment.actions[1].options[0].value, "rage>=80")
        self.assertEqual(document.issues, ())

    def test_ranges_cover_assignment_action_option_and_option_value(self):
        source = "  actions.foo+=/spell,if=(rage<80|buff.x.up),name=bar"

        assignment = parse(source).lines[0]
        action = assignment.actions[0]
        condition, name = action.options

        self.assertEqual(assignment.range, SourceRange(
            SourcePosition(1, 3), SourcePosition(1, 54)))
        self.assertEqual(assignment.list_name_range, SourceRange(
            SourcePosition(1, 11), SourcePosition(1, 14)))
        self.assertEqual(action.range, SourceRange(
            SourcePosition(1, 17), SourcePosition(1, 54)))
        self.assertEqual(action.name_range, SourceRange(
            SourcePosition(1, 17), SourcePosition(1, 22)))
        self.assertEqual(condition.range, SourceRange(
            SourcePosition(1, 23), SourcePosition(1, 45)))
        self.assertEqual(condition.value_range, SourceRange(
            SourcePosition(1, 26), SourcePosition(1, 45)))
        self.assertEqual(name.value_range, SourceRange(
            SourcePosition(1, 51), SourcePosition(1, 54)))

    def test_slashes_inside_parenthesized_or_quoted_values_do_not_split_actions(self):
        document = parse(
            'actions=/first,if=func(foo/bar),label="a/b"/second,if=ready'
        )

        assignment = document.lines[0]
        self.assertEqual([entry.name for entry in assignment.actions], ["first", "second"])
        self.assertEqual(assignment.actions[0].options[0].value, "func(foo/bar)")
        self.assertEqual(assignment.actions[0].options[1].value, '"a/b"')

    def test_mismatched_closing_bracket_does_not_expose_an_action_separator(self):
        document = parse("actions=/foo,if=(a]/bar")

        assignment = document.lines[0]
        self.assertIsInstance(assignment, ActionAssignment)
        self.assertEqual([entry.name for entry in assignment.actions], ["foo"])
        self.assertEqual(assignment.actions[0].options[0].value, "(a]/bar")
        self.assertEqual(
            [issue.code for issue in document.issues],
            ["mismatched-closing-bracket", "unclosed-bracket"],
        )

    def test_invalid_option_preserves_assignment_and_parsed_action_prefix(self):
        document = parse("actions=/charge/execute,if")

        assignment = document.lines[0]
        self.assertIsInstance(assignment, ActionAssignment)
        self.assertEqual([entry.name for entry in assignment.actions], ["charge", "execute"])
        execute = assignment.actions[1]
        self.assertEqual(execute.name_range, SourceRange(
            SourcePosition(1, 17), SourcePosition(1, 24)))
        self.assertIsInstance(execute.options[0], InvalidOption)
        self.assertEqual(execute.options[0].raw, "if")
        self.assertEqual(execute.options[0].code, "invalid-option")
        self.assertEqual(document.issues, (execute.options[0].issue,))

    def test_invalid_action_segment_preserves_prefix_and_later_actions(self):
        document = parse("actions=/charge//execute")

        assignment = document.lines[0]
        self.assertIsInstance(assignment, ActionAssignment)
        self.assertEqual(len(assignment.actions), 3)
        self.assertEqual(assignment.actions[0].name, "charge")
        self.assertIsInstance(assignment.actions[1], InvalidActionEntry)
        self.assertEqual(assignment.actions[1].raw, "")
        self.assertEqual(assignment.actions[2].name, "execute")
        self.assertEqual(document.issues, (assignment.actions[1].issue,))

    def test_escaped_quote_does_not_expose_a_quoted_slash(self):
        document = parse(r'actions=/first,label="a\"/b"/second')

        assignment = document.lines[0]
        self.assertEqual([entry.name for entry in assignment.actions], ["first", "second"])
        self.assertEqual(document.issues, ())

    def test_unclosed_quote_is_invalid_without_splitting_a_quoted_slash(self):
        source = 'actions=/foo,if="a/b'

        tokens = lex(source)
        document = parse(source)

        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].kind, LineKind.INVALID)
        self.assertIsInstance(document.lines[0], InvalidLine)
        self.assertEqual(len(document.issues), 1)
        self.assertEqual(document.issues[0].code, "invalid-line")

    def test_document_end_uses_the_same_line_boundaries_as_the_lexer(self):
        for separator in ("\r\n", "\r", "\u2028"):
            with self.subTest(separator=repr(separator)):
                source = f"actions=first{separator}actions=second"

                tokens = lex(source)
                document = parse(source)

                self.assertEqual([token.range.start.line for token in tokens], [1, 2])
                self.assertEqual(document.range.end, tokens[-1].range.end)

    def test_empty_document_and_consecutive_trailing_newlines(self):
        empty = parse("")
        trailing = parse("actions=first\n\n")

        self.assertEqual(empty.lines, ())
        self.assertEqual(empty.range, SourceRange(
            SourcePosition(1, 1), SourcePosition(1, 1)))
        self.assertEqual(len(trailing.lines), 2)
        self.assertIsInstance(trailing.lines[1], BlankLine)
        self.assertEqual(trailing.range.end, SourcePosition(3, 1))

    def test_invalid_lines_and_malformed_actions_are_diagnostic_nodes(self):
        document = parse("player=warrior\nactions.foo\nactions+=/\n")

        self.assertTrue(all(isinstance(line, InvalidLine) for line in document.lines))
        self.assertEqual(len(document.issues), 3)
        self.assertEqual(document.issues[0].range, SourceRange(
            SourcePosition(1, 1), SourcePosition(1, 15)))
        self.assertTrue(all(issue.code == "invalid-line" for issue in document.issues))

    def test_current_upstream_default_apl_corpus_smoke(self):
        # Representative lines captured from the current upstream
        # ActionPriorityLists/default corpus. Keeping the fixture local makes
        # this test independent of a production SimC checkout/server.
        action_lines = [
            "actions.precombat+=/snapshot_stats",
            "actions+=/run_action_list,name=default,if=(active_enemies>=3|buff.test.up)",
            "actions.cds=variable,name=sync,value=talent.foo&(time=0|fight_remains<15)",
            "actions.finish+=/killing_spree,interrupt_if=energy.time_to_max<2,interrupt_global=1",
            "actions.aoe+=/spell,target_if=min:debuff.foo.remains,if=!buff.bar.up",
            # Upstream currently has one tolerated trailing comma.
            "actions.precombat+=/variable,name=trinket_2_duration,value=trinket.2.proc.any_dps.duration,",
        ]

        for line in action_lines:
            with self.subTest(line=line):
                document = parse(line)
                self.assertEqual(document.issues, ())
                self.assertIsInstance(document.lines[0], ActionAssignment)
