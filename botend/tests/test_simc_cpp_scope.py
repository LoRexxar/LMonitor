"""验证伤害函数的原生类型边界，防止治疗或模板参数冒充技能范围。"""
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from simc_cpp_scope import class_spans, non_damage_read, generic_action


class CppScopeTests(unittest.TestCase):
    def test_template_parameter_is_not_enclosing_action(self):
        text='template<class Base>\nstruct spell_action_t : public Base { double action_multiplier() { return 1; } };'
        self.assertEqual([s[2] for s in class_spans(text)],['spell_action_t'])

    def test_braces_inside_comments_and_strings_do_not_change_scope(self):
        text='struct skill_t { const char* s="}"; /* } */ void run() {} };'
        spans=class_spans(text)
        self.assertEqual(len(spans),1)
        self.assertEqual(spans[0][1],text.index('};')+1)

    def test_healing_inheritance_is_not_damage(self):
        self.assertTrue(non_damage_read({'父类':': public wrapper_t<druid_heal_t>', '类型声明':''}))

    def test_self_target_damage_is_not_enemy_damage(self):
        self.assertTrue(non_damage_read({'父类':': public spell_t', '类型声明':'struct damage_t { damage_t() { target = player; }'}))

    def test_external_action_concrete_damage_is_not_generic_player_scope(self):
        self.assertFalse(generic_action({'父类':': public evoker_external_action_t<spell_t>', '代码':'return talent.value();'}))
