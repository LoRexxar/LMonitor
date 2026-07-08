import unittest

from botend.wow.spell_text import SpellTextResolver
from botend.wow.talents.metadata import TalentMetadataProvider


class _FakeEffectResolver(SpellTextResolver):
    def __init__(self, effect_map):
        super().__init__(locale='zhCN', branch='wow')
        self.effect_map = effect_map

    def _effects(self, spell_id: int):
        return self.effect_map.get(spell_id, {})


class _FakeSnapshotResolver(_FakeEffectResolver):
    def __init__(self, effect_map, spell_map):
        super().__init__(effect_map)
        self.spell_map = spell_map

    def _spell_snapshot(self, spell_id: int):
        return self.spell_map.get(spell_id, {})


class SpellTextResolverTests(unittest.TestCase):
    def test_blizzard_effect_placeholders_are_one_based(self):
        resolver = _FakeEffectResolver({
            391477: {
                0: {'base_points': '5'},
                1: {'base_points': '9'},
            }
        })

        self.assertEqual(resolver.resolve('伤害提高$s1%，第二段$s2%。', 391477), '伤害提高5%，第二段9%。')

    def test_external_spell_effect_reference_in_expression_uses_first_effect(self):
        resolver = _FakeEffectResolver({
            221322: {
                0: {'base_points': '30'},
            }
        })

        self.assertEqual(resolver.resolve('产生${$221322s1/10}点符文能量。', 207104), '产生3点符文能量。')

    def test_expression_supports_abs_function(self):
        resolver = _FakeEffectResolver({
            440290: {
                0: {'base_points': '-25'},
            }
        })

        self.assertEqual(resolver.resolve('降低${$abs($440290s1/10)}.1%。', 440282), '降低2.5%。')

    def test_duration_and_stack_count_remain_readable_fallbacks_when_tables_missing(self):
        resolver = _FakeEffectResolver({
            391481: {
                0: {'base_points': '10'},
            }
        })

        # $d resolves from SpellDuration CSV, $u falls back to empty string
        result = resolver.resolve('提高$391481s1%，持续$391481d，最多叠加$391481u次。', 391477)
        self.assertIn('提高10%', result)
        self.assertIn('次。', result)
        # $u should not leave raw token
        self.assertNotIn('$', result)

    def test_cleanup_blizzard_conditionals_without_leaking_tokens(self):
        resolver = _FakeEffectResolver({})

        text = '造成10?$c2[20][50]%伤害。技能?a137019[火焰][冰霜]提高。'

        self.assertEqual(resolver.resolve(text, 1), '造成1020%伤害。技能火焰提高。')

    def test_cleanup_conditionals_even_without_dollar_tokens(self):
        resolver = _FakeEffectResolver({})

        text = '炽热连击?a137020[冰冷智慧][节能施法]的持续时间延长10秒。'

        self.assertEqual(resolver.resolve(text, 1), '炽热连击冰冷智慧的持续时间延长10秒。')

    def test_cleanup_conditionals_with_negated_terms(self):
        resolver = _FakeEffectResolver({})

        text = '神圣之火?a137031&!s14914[神圣之火和暗言术：痛][暗言术：痛]的伤害提高10%。'

        self.assertEqual(resolver.resolve(text, 1), '神圣之火神圣之火和暗言术：痛的伤害提高10%。')

    def test_talent_metadata_provider_resolves_question_conditionals(self):
        provider = TalentMetadataProvider()
        resolver = _FakeEffectResolver({})

        text = '炽热连击?a137020[冰冷智慧][节能施法]的持续时间延长10秒。'

        self.assertEqual(
            provider._resolve_text(resolver, text, 1),
            '炽热连击冰冷智慧的持续时间延长10秒。',
        )

    def test_cleanup_orphan_question_marker(self):
        resolver = _FakeEffectResolver({})

        self.assertEqual(
            resolver.resolve('造成0点$?物理伤害。', 404542),
            '造成0点物理伤害。',
        )

    def test_inline_division_placeholder_is_resolved_or_hidden(self):
        resolver = _FakeEffectResolver({
            376080: {
                2: {'base_points': '150'},
            },
        })

        self.assertEqual(
            resolver.resolve('产生$/10;376080s3点怒气。', 376079),
            '产生15点怒气。',
        )
        self.assertEqual(
            resolver.resolve('产生$/100;s2点狂乱值。', 1227280),
            '产生x点狂乱值。',
        )

    def test_cleanup_b_placeholder_without_leaking_token(self):
        resolver = _FakeEffectResolver({})

        self.assertEqual(
            resolver.resolve('有$b1%几率获得一个额外的连击点数。', 14161),
            '有x%几率获得一个额外的连击点数。',
        )

    def test_cleanup_dynamic_tokens_without_raw_dollar_output(self):
        resolver = _FakeSnapshotResolver(
            {},
            {
                1253601: {'aura_description': '每层造成额外伤害。'},
                1269383: {'description': '用英勇打击攻击目标。'},
            },
        )

        text = '有$h%的几率触发。每$proccooldown秒一次。$@switch<1>[几率][更高几率] $@spellaura1253601'

        self.assertEqual(
            resolver.resolve(text, 1),
            '有点%的几率触发。每一段时间秒一次。几率 每层造成额外伤害。',
        )
        self.assertEqual(
            resolver.resolve('投掷$n枚战轮，共造成$x次伤害。$@spelltooltip1269383', 1),
            '投掷x枚战轮，共造成x次伤害。用英勇打击攻击目标。',
        )
