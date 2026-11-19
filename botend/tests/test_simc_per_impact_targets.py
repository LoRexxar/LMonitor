"""用狂暴战原生导出回归逐目标施加的流血，保留输入和结果供复查。"""

import json
import os
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from unittest import TestCase, skipUnless


@skipUnless(os.environ.get('SIMC_70575_TEST_BINARY'), '需要 70575 版本的原生技能导出器')
class PerImpactTargetTests(TestCase):
    def test_bloodbath_dot_uses_each_parent_impact_as_a_separate_application(self):
        artifact_root = Path(__file__).resolve().parents[2] / 'tmp'
        artifact_root.mkdir(exist_ok=True)
        output = Path(mkdtemp(prefix='simc-bloodbath-', dir=artifact_root))
        report = output / 'export.json'
        profile = output / 'fury.simc'
        profile.write_text(
            'warrior=regression\nspec=fury\nlevel=90\nrace=dwarf\n'
            'main_hand=,id=268213,ilevel=344\noff_hand=,id=237847,ilevel=331\n'
            'talents=CgEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgGDjZMz2yMzMjZmxMzMzMjZWmZmZmxsZmZGAAIMwGssY0YGQmFMjFAzgBAMzAwwiZAGD\n'
            'actions=wait\n', encoding='utf-8',
        )
        subprocess.run([
            os.environ['SIMC_70575_TEST_BINARY'], str(profile), f'skill_damage_export={report}',
            'skill_damage_game_build=12.1.5.70575',
            'skill_damage_revision=30c4b061f2103132a34952d80c963303b82bf368',
        ], capture_output=True, check=True, timeout=90)
        actor = json.loads(report.read_bytes())['actors'][0]
        components = {row['token']: row for row in actor['actions'] if row['reporting_root_token'] == 'bloodbath'}
        for token, kind in [('bloodbath', 'direct'), ('bloodbath_dot', 'tick')]:
            action = components[token]
            cleave = next(state['values'][kind] for state in action['scenarios']
                          if [buff['token'] for buff in state['buffs']] == ['buff.whirlwind'])
            base = action['baseline'][kind]
            for targets in (1, 2, 5, 10, 20):
                # 法术 85739 的额外四个目标承受 65% 直接伤害；各流血独立施加。
                factor = 1 + min(targets - 1, 4) * (0.65 if kind == 'direct' else 1)
                for field in ('target_hit', 'target_crit', 'target_expected',
                              'target_noncrit_contribution', 'target_crit_contribution'):
                    with self.subTest(token=token, targets=targets, field=field):
                        self.assertAlmostEqual(base[field][str(targets)], base[field]['1'])
                        self.assertAlmostEqual(cleave[field][str(targets)], base[field]['1'] * factor)
