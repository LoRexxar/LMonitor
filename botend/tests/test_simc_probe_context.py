"""未提供执行上下文的成员不能因当前内存恰好为零而通过检查。"""
import importlib.util
from pathlib import Path
from unittest import TestCase, skipUnless


@skipUnless(importlib.util.find_spec('tree_sitter') and importlib.util.find_spec('tree_sitter_cpp'), '需要本地C++语法树依赖')
class ProbeContextAuditTests(TestCase):
    def inspect(self, code):
        from scripts.audit_simc_probe_context import audit_source
        class MemoryFile:
            suffix = '.cpp'
            def read_bytes(self):
                return code.encode()
            def relative_to(self, root):
                return Path('fixture.cpp')
        class MemoryRoot:
            def rglob(self, pattern):
                return [MemoryFile()]
        return audit_source(MemoryRoot())[1]

    def test_direct_damage_member_requires_initialization_or_an_explicit_context(self):
        body = 'struct child_t { double amount; child_t() {} double action_multiplier() const { return amount; } };'
        rows = self.inspect(body)
        self.assertEqual(rows[0]['成员'], 'amount')
        self.assertFalse(rows[0]['已有上下文标记'])
        self.assertEqual(self.inspect(body.replace('child_t() {}', 'child_t() : amount(1.0) {}')), [])
        rows = self.inspect(body.replace('child_t() {}', 'child_t() { snapshot_state_requires_parent_execute_state = true; }'))
        self.assertTrue(rows[0]['已有上下文标记'])

    def test_macro_and_template_constructors_are_not_misreported_as_missing_initialization(self):
        self.assertEqual(self.inspect('struct child_t { double amount; DRUID_ABILITY(child_t, base_t, "x", data), amount(1.0) {} double action_multiplier() const { return amount; } };'), [])
        self.assertEqual(self.inspect('struct child_t { double amount; template<class T> child_t(T value) : amount(value) {} double action_multiplier() const { return amount; } };'), [])

    def test_local_variable_does_not_create_a_false_member_dependency(self):
        self.assertEqual(self.inspect('struct child_t { double amount; child_t() {} double action_multiplier() const { double amount = 1.0; return amount; } };'), [])


class ProbeStabilityTests(TestCase):
    def test_unresolved_native_context_is_visible_instead_of_silently_omitted(self):
        from botend.services.simc_skill_damage import collect_skill_damage_unresolved
        action = {'token': 'child', 'spell_id': 11, 'supported': False, 'harmful': True,
                  'player_skill': True, 'baseline': {'unresolved_reason': 'child_trigger_dependent'}}
        rows = collect_skill_damage_unresolved({'actors': [{'class': 'warrior', 'spec': 'arms', 'actions': [action]}]})
        self.assertEqual(rows[0]['reason'], 'child_trigger_dependent')
        action['player_skill'] = False
        self.assertEqual(collect_skill_damage_unresolved({'actors': [{'actions': [action]}]}), [])

    def test_repeated_process_comparison_rejects_different_damage_and_missing_conditions(self):
        from scripts.generate_simc_reviewed_talent_probes import assert_repeat_stable, ProbeStabilityError
        value = {'amount': 566.5725, 'states': [{'spell_id': 11}]}
        assert_repeat_stable(value, {'amount': 566.572500000001, 'states': [{'spell_id': 11}]})
        with self.assertRaises(ProbeStabilityError):
            assert_repeat_stable(value, {'amount': 9.5e-311, 'states': [{'spell_id': 11}]})
        with self.assertRaises(ProbeStabilityError):
            assert_repeat_stable(value, {'amount': 566.5725, 'states': []})
