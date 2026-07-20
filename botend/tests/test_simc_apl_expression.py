from django.test import SimpleTestCase

from botend.services.simc_apl import parse
from botend.services.simc_apl.ast import (
    BinaryExpression,
    CallExpression,
    IdentifierExpression,
    NumberExpression,
    SourcePosition,
    SourceRange,
    UnaryExpression,
)


class SimcAplExpressionTests(SimpleTestCase):
    def option(self, source, index=0):
        return parse(source).lines[-1].actions[0].options[index]

    def test_simc_precedence_and_left_associativity(self):
        expression = self.option("actions=x,if=a|b^c&d=e+f*g").expression
        self.assertEqual(expression.operator, "|")
        self.assertEqual(expression.right.operator, "^")
        self.assertEqual(expression.right.right.operator, "&")
        self.assertEqual(expression.right.right.right.operator, "=")
        self.assertEqual(expression.right.right.right.right.operator, "+")
        self.assertEqual(expression.right.right.right.right.right.operator, "*")

        subtraction = self.option("actions=x,if=a-b-c").expression
        self.assertEqual(subtraction.operator, "-")
        self.assertEqual(subtraction.left.operator, "-")

    def test_special_simc_operators_have_real_meaning_and_precedence(self):
        expression = self.option("actions=x,if=a%b%%c<?d>?e").expression
        self.assertEqual(expression.operator, ">?")
        self.assertEqual(expression.left.operator, "<?")
        self.assertEqual(expression.left.left.operator, "%%")
        self.assertEqual(expression.left.left.left.operator, "%")
        self.assertEqual(document := parse("actions=x,if=a%b%%c<?d>?e").issues, ())

    def test_upstream_constructible_unary_dotted_numbers_and_functions(self):
        source = "actions=x,if=!buff.foo.up+-1+@variable.x+floor(1.5)+ceil(variable.x)"
        expression = self.option(source).expression
        self.assertIsInstance(expression, BinaryExpression)
        self.assertEqual(parse(source).issues, ())
        self.assertIsInstance(expression.right, CallExpression)

    def test_consecutive_unary_matches_upstream_shunting_yard(self):
        for value in ("!!foo", "!-foo", "-@foo", "+!foo", "@@foo"):
            with self.subTest(value=value):
                self.assertIn(
                    "invalid-consecutive-unary",
                    [issue.code for issue in parse(f"actions=x,if={value}").issues],
                )
        # A minus immediately followed by a number is folded into TOK_NUM by upstream.
        for value in ("--1", "!-1", "@-1", "!(-foo)"):
            with self.subTest(value=value):
                self.assertEqual(parse(f"actions=x,if={value}").issues, ())

    def test_target_if_selector_and_plain_target_if(self):
        selected = self.option("actions=x,target_if=min:dot.foo.remains")
        plain = self.option("actions=x,target_if=refreshable")
        first = self.option("actions=x,target_if=first:target.health.pct<20")
        self.assertEqual(selected.expression_selector, "min")
        self.assertEqual(selected.expression.name, "dot.foo.remains")
        self.assertIsNone(plain.expression_selector)
        self.assertEqual(plain.expression.name, "refreshable")
        self.assertEqual(first.expression_selector, "first")

    def test_target_if_selectors_and_upstream_xor_alias(self):
        source = "  actions=x,target_if=MiN:dot.foo.remains"
        option = self.option(source)
        self.assertEqual(option.expression_selector, "min")
        self.assertEqual(option.selector_range, SourceRange(
            SourcePosition(1, 23), SourcePosition(1, 26)))
        self.assertEqual(option.expression.range, SourceRange(
            SourcePosition(1, 27), SourcePosition(1, 42)))

        document = parse("actions=x,target_if=median:dot.foo.remains")
        self.assertEqual([issue.code for issue in document.issues], [
            "invalid-target-if-selector",
        ])
        self.assertEqual(document.issues[0].range, SourceRange(
            SourcePosition(1, 21), SourcePosition(1, 27)))
        self.assertEqual(document.lines[0].actions[0].options[0].expression.name,
                         "dot.foo.remains")

        document = parse("actions=x,if=a^^b")
        self.assertEqual(document.issues, ())
        self.assertEqual(document.lines[0].actions[0].options[0].expression.operator, "^")

    def test_expression_options_only_and_variable_value_context(self):
        source = (
            "actions=variable,name=a+b,op=set,value=x+1,value_else=y-1,condition=z,if=q,sec=0.05\n"
            "actions+=/spell,label=a+b,interrupt_if=ticks>=3,cancel_if=gcd.remains=0"
        )
        document = parse(source)
        first = document.lines[0].actions[0].options
        second = document.lines[1].actions[0].options
        self.assertIsNone(first[0].expression)  # name
        self.assertIsNone(first[1].expression)  # op
        self.assertTrue(all(option.expression is not None for option in first[2:6]))
        self.assertIsNone(first[6].expression)  # sec is wait-only
        self.assertIsNone(second[0].expression)  # label must not be linted
        self.assertTrue(all(option.expression is not None for option in second[1:]))
        self.assertEqual(document.issues, ())

    def test_expression_scope_and_division_disambiguation(self):
        source = (
            "actions=cycling_variable,value=x+1,value_else=y-1,condition=z\n"
            "actions+=/wait,sec=gcd.remains\n"
            "actions+=/spell,condition=not+parsed,sec=also+not+parsed,target_if=x"
        )
        document = parse(source)
        cycling = document.lines[0].actions[0].options
        wait = document.lines[1].actions[0].options
        spell = document.lines[2].actions[0].options
        self.assertTrue(all(option.expression is not None for option in cycling))
        self.assertIsNotNone(wait[0].expression)
        self.assertIsNone(spell[0].expression)
        self.assertIsNone(spell[1].expression)
        self.assertIsNotNone(spell[2].expression)
        self.assertEqual(document.issues, ())

        typo = parse("actions=/x,if=a/b")
        issue = next(issue for issue in typo.issues
                     if issue.code == "invalid-division-operator")
        self.assertEqual((issue.suggestion, issue.range), ("%", SourceRange(
            SourcePosition(1, 16), SourcePosition(1, 17))))
        self.assertEqual(typo.lines[0].actions[0].options[0].value, "a/b")

        legal = parse("actions=/x,if=a/next_action,if=b")
        self.assertEqual([action.name for action in legal.lines[0].actions],
                         ["x", "next_action"])
        self.assertNotIn("invalid-division-operator",
                         [issue.code for issue in legal.issues])

    def test_value_is_not_expression_for_non_variable_action(self):
        document = parse("actions=some_action,value=not/an/expression")
        self.assertEqual(document.issues, ())
        self.assertIsNone(document.lines[0].actions[0].options[0].expression)

    def test_empty_expression_and_empty_target_selector(self):
        for source in ("actions=x,if=", "actions=x,target_if=min:"):
            with self.subTest(source=source):
                document = parse(source)
                self.assertEqual(document.issues[0].code, "empty-expression")
                self.assertIsNotNone(document.lines[0].actions[0])

    def test_forbidden_aliases_have_precise_ranges_and_suggestions(self):
        source = "# offset\n  actions=x,if=a==b&&c||d"
        document = parse(source)
        self.assertEqual([issue.code for issue in document.issues], [
            "invalid-equality-operator", "invalid-logical-operator", "invalid-logical-operator",
        ])
        self.assertEqual([issue.suggestion for issue in document.issues], ["=", "&", "|"])
        self.assertEqual([issue.range for issue in document.issues], [
            SourceRange(SourcePosition(2, 17), SourcePosition(2, 19)),
            SourceRange(SourcePosition(2, 20), SourcePosition(2, 22)),
            SourceRange(SourcePosition(2, 23), SourcePosition(2, 25)),
        ])

    def test_unknown_token_parentheses_and_missing_operands(self):
        cases = {
            "actions=x,if=a$b": "unknown-expression-token",
            "actions=x,if=(a+b": "unclosed-parenthesis",
            "actions=x,if=a+b)": "mismatched-parenthesis",
            "actions=x,if=a+": "missing-expression-operand",
            "actions=x,if=a+*b": "missing-expression-operand",
            "actions=x,if=a b": "missing-expression-operator",
            "actions=x,if=()": "missing-expression-operand",
        }
        for source, code in cases.items():
            with self.subTest(source=source):
                document = parse(source)
                self.assertIn(code, [issue.code for issue in document.issues])
                self.assertEqual(document.lines[0].actions[0].name, "x")

    def test_absolute_multiline_range_for_missing_operand(self):
        document = parse("actions=first\n    actions=x,if=a+")
        issue = next(issue for issue in document.issues if issue.code == "missing-expression-operand")
        self.assertEqual(issue.range, SourceRange(
            SourcePosition(2, 19), SourcePosition(2, 20)))

    def test_inline_comment_stops_expression_parsing_and_is_a_warning(self):
        source = "actions=/x,if=a# comment"

        document = parse(source)
        option = document.lines[0].actions[0].options[0]

        self.assertIsInstance(option.expression, IdentifierExpression)
        self.assertEqual(option.expression.name, "a")
        self.assertEqual([issue.code for issue in document.issues], ["inline-comment"])
        self.assertEqual(document.issues[0].severity, "warning")
        self.assertEqual(document.issues[0].range, SourceRange(
            SourcePosition(1, 16), SourcePosition(1, 25)))

    def test_hash_inside_quotes_is_not_an_inline_comment(self):
        document = parse('actions=/x,label="# literal",if=a')

        self.assertEqual(document.issues, ())
        self.assertEqual(document.lines[0].actions[0].options[0].value, '"# literal"')
        self.assertEqual(document.lines[0].actions[0].options[1].expression.name, "a")

    def test_extreme_unary_and_parenthesis_depth_is_diagnostic_not_exception(self):
        for expression in ("!" * 2000 + "a", "(" * 2000 + "a" + ")" * 2000):
            with self.subTest(prefix=expression[:10]):
                document = parse(f"actions=x,if={expression}")
                self.assertIn("expression-too-deep",
                              [issue.code for issue in document.issues])
