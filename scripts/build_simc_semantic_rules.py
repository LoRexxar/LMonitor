"""把已复核的公共选择器编译为运行时规则，不生成技能名称或法术白名单。"""
import argparse
from pathlib import Path

from simc_scope_resolution import COMMON_SELECTORS, CATEGORY_SELECTORS, REVIEWED_REVISION


def render_rules():
    rows = [(family, word, bit) for family, (word, bit, _) in COMMON_SELECTORS.items()]
    rows += [(family, word, bit) for family, selectors in CATEGORY_SELECTORS.items()
             for word, bit, _ in selectors]
    return '\n'.join([
        '// 由 build_simc_semantic_rules.py 生成；选择器有版本约束，不包含技能白名单。',
        '#pragma once',
        f'constexpr const char* skill_damage_selector_revision = "{REVIEWED_REVISION}";',
        'struct skill_damage_semantic_selector_t { unsigned family, word, mask; };',
        'constexpr skill_damage_semantic_selector_t skill_damage_semantic_selectors[] = {',
        *[f'  {{{family}, {word}, {mask}u}},' for family, word, mask in sorted(set(rows))],
        '};', '',
    ])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render_rules(), encoding='utf-8', newline='\n')
