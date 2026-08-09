import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from botend.management.commands.generate_simc_benchmark_tooltips import (
    _required_spell_ids,
    _query_simc,
    _simc_revision,
)
from botend.services.simc_benchmark_tooltip_generator import (
    parse_simc_spell_query,
    render_spell_description,
)


MAIN_SPELL_OUTPUT = """Name             : First Mate's Shellward (id=1295328)
Duration         : 30 seconds
Effects          :
#1 (id=1317317)  : Apply Aura (6) | Absorb Damage (69)
                   Base Value: 0 | Scaled Value: 0 | Misc Value: 0x7f
#2 (id=1317323)  : Apply Aura (6) | Dummy (4)
                   Base Value: 40 | Scaled Value: 0
#3 (id=1317324)  : Apply Aura (6) | Dummy (4)
                   Base Value: 80 | Scaled Value: 0
#4 (id=1317325)  : Apply Aura (6) | Dummy (4)
                   Base Value: 40 | Scaled Value: 0
"""

SCALING_SPELL_OUTPUT = """Name             : First Mate's Shellward (id=1295323) [Passive]
Effects          :
#1 (id=1317305)  : School Damage (2)
                   Base Value: 0 | Scaled Value: 332374.7 (coefficient=687.1891)
#2 (id=1317344)  : School Damage (2)
                   Base Value: 0 | Scaled Value: 13229.16 (coefficient=27.35146)
"""

PERIODIC_SPELL_OUTPUT = """Name             : Noxious Venom (id=267410)
Duration         : 4 seconds
Stacks           : 3 maximum
Effects          :
#1 (id=714074)   : Apply Aura (6) | Periodic Damage (3): nature every 1 seconds | Scaling Class: Replace Secondary (-9)
                   Base Value: 0 | Scaled Value: 10337.87 (coefficient=17.71982)
"""


class SimcBenchmarkTooltipGeneratorTests(unittest.TestCase):
    def test_parses_scaled_and_fixed_effect_values(self):
        main = parse_simc_spell_query(MAIN_SPELL_OUTPUT)
        scaling = parse_simc_spell_query(SCALING_SPELL_OUTPUT)

        self.assertEqual(main['duration_seconds'], 30)
        self.assertEqual(main['effects'][2]['base_value'], 40)
        self.assertEqual(main['effects'][2]['scaled_value'], 0)
        self.assertEqual(scaling['effects'][1]['scaled_value'], 332374.7)
        self.assertEqual(scaling['effects'][2]['scaled_value'], 13229.16)

    def test_renders_exact_item_level_values_from_referenced_spell(self):
        template = (
            '部署甲壳结界，在$d内吸收最多$s2%的承受伤害，直至累计化解'
            '$1295323s1点伤害。生命值低于$s4%时，减伤提高至$s3%。\n\n'
            '对近战攻击者造成$1295323s2点物理伤害。'
        )
        rendered, unresolved = render_spell_description(
            template,
            base_spell_id=1295328,
            spell_queries={
                1295328: parse_simc_spell_query(MAIN_SPELL_OUTPUT),
                1295323: parse_simc_spell_query(SCALING_SPELL_OUTPUT),
            },
        )

        self.assertEqual(unresolved, [])
        self.assertEqual(
            rendered,
            '部署甲壳结界，在30秒内吸收最多40%的承受伤害，直至累计化解'
            '332,375点伤害。生命值低于40%时，减伤提高至80%。\n\n'
            '对近战攻击者造成13,229点物理伤害。',
        )

    def test_renders_spell_references_for_duration_stacks_ticks_and_effect_values(self):
        periodic = parse_simc_spell_query(PERIODIC_SPELL_OUTPUT)
        self.assertEqual(periodic['duration_seconds'], 4)
        self.assertEqual(periodic['max_stacks'], 3)
        self.assertEqual(periodic['effects'][1]['period_seconds'], 1)

        rendered, unresolved = render_spell_description(
            '在$267410d内造成$267410w1点伤害，每$267410t1秒一次，最多叠加$267410u层。',
            base_spell_id=1295328,
            spell_queries={267410: periodic},
        )

        self.assertEqual(unresolved, [])
        self.assertEqual(rendered, '在4秒内造成10,338点伤害，每1秒一次，最多叠加3层。')

    def test_renders_constant_arithmetic_expressions(self):
        rendered, unresolved = render_spell_description(
            '最多可获得${23.32*15}爆击，持续${5秒*4}秒。',
            base_spell_id=1,
            spell_queries={},
        )

        self.assertEqual(unresolved, [])
        self.assertEqual(rendered, '最多可获得349.8爆击，持续20秒。')

    def test_renders_referenced_spell_descriptions_in_their_own_context(self):
        referenced = """Effects          :
#1 (id=1)       : Dummy
                   Base Value: 25 | Scaled Value: 0
#2 (id=2)       : Dummy
                   Base Value: 8 | Scaled Value: 0
"""
        rendered, unresolved = render_spell_description(
            '造成伤害。$@spelldesc1240903',
            base_spell_id=10,
            spell_queries={1240903: parse_simc_spell_query(referenced)},
            spell_descriptions={1240903: '每额外击中一个敌人，伤害提高$s1%，最多${$s1*$s2}%。'},
        )

        self.assertEqual(unresolved, [])
        self.assertEqual(rendered, '造成伤害。每额外击中一个敌人，伤害提高25%，最多200%。')

    def test_collects_required_spells_through_description_references(self):
        required = _required_spell_ids(
            '造成伤害。$@spelldesc1240903',
            10,
            {
                1240903: '伤害提高$s1%，并触发$200s2。',
            },
        )

        self.assertEqual(required, {10, 1240903, 200})

    def test_reads_simc_revision_from_the_nearest_repository_ancestor(self):
        with TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            subprocess.run(['git', 'init', '-q'], cwd=repository, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repository, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repository, check=True)
            binary = repository / 'build' / 'simc'
            binary.parent.mkdir()
            binary.write_text('', encoding='utf-8')
            subprocess.run(['git', 'add', 'build/simc'], cwd=repository, check=True)
            subprocess.run(['git', 'commit', '-qm', 'fixture'], cwd=repository, check=True)
            expected = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=repository, check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()

            self.assertEqual(_simc_revision(binary), expected)

    def test_simc_query_cache_isolated_by_build(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary = root / 'simc'
            binary.write_text('', encoding='utf-8')
            cache_dir = root / 'cache'
            output = 'World of Warcraft 12.1.0.69189 PTR\nEffects:\n#1 (id=1) : Dummy\n'
            completed = subprocess.CompletedProcess(
                args=['simc'], returncode=0, stdout=output,
            )
            with patch(
                'botend.management.commands.generate_simc_benchmark_tooltips.subprocess.run',
                return_value=completed,
            ) as run:
                _query_simc(binary, cache_dir, 1, 285, '12.1.0.69189')
                _query_simc(binary, cache_dir, 1, 285, '12.1.0.69189')

            self.assertEqual(run.call_count, 1)
            self.assertTrue(
                (cache_dir / 'simc' / '12.1.0.69189'
                 / 'spell-1-ilevel-285.txt').is_file()
            )

    def test_preserves_and_reports_unsupported_tokens(self):
        rendered, unresolved = render_spell_description(
            '获得$w1点护盾，并触发$?a1[效果甲][效果乙]。',
            base_spell_id=1295328,
            spell_queries={1295328: parse_simc_spell_query(MAIN_SPELL_OUTPUT)},
        )

        self.assertEqual(rendered, '获得$w1点护盾，并触发$?a1[效果甲][效果乙]。')
        self.assertEqual(unresolved, ['$w1', '$?a1[效果甲][效果乙]'])


if __name__ == '__main__':
    unittest.main()