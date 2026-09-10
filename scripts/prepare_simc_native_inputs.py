"""从指定 SimC 源码的最新赛季模板生成基础和预设天赋审计输入。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from botend.constants.simc_specs import SIMC_KNOWN_SPECS


def discover_profiles(source):
    """优先最新赛季，同专精优先无额外变体后缀的模板。"""
    profiles = {}
    seasons = sorted((p for p in (source / 'profiles').glob('MID*')
                      if p.is_dir() and re.fullmatch(r'MID\d+', p.name)),
                     key=lambda p: int(p.name.removeprefix('MID')), reverse=True)
    for season in seasons:
        for path in sorted(season.glob('*.simc'), key=lambda p: (len(p.name), p.name)):
            content = path.read_text(encoding='utf-8')
            cls = re.search(r'^(\w+)="[^"]+"', content, re.M)
            spec = re.search(r'^spec=(\w+)', content, re.M)
            if cls and spec:
                profiles.setdefault((cls[1], spec[1]), (path, content))
    return profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for kind in ('base', 'talents'):
        (args.output / kind).mkdir()
    profiles = discover_profiles(args.source)
    rows = []
    for cls, specs in sorted(SIMC_KNOWN_SPECS.items()):
        for spec in sorted(specs):
            profile = profiles.get((cls, spec))
            content = profile[1] if profile else f'{cls}="audit"\nspec={spec}\nlevel=90\n'
            lines = [line for line in content.splitlines()
                     if re.match(r'^(?:' + cls + r'|spec|level|race|main_hand|off_hand)=', line)]
            lines += ['role=attack', 'actions=wait']
            base = 'iterations=1\nthreads=1\nallow_experimental_specializations=1\n' + '\n'.join(lines) + '\n'
            talents = '\n'.join(line for line in content.splitlines()
                                if line.startswith(('talents=', 'class_talents=', 'spec_talents=', 'hero_talents=')))
            for kind, text in [('base', base), ('talents', base + talents + '\n')]:
                (args.output / kind / f'{cls}_{spec}.simc').write_text(text, encoding='utf-8')
            rows.append({'职业': cls, '专精': spec,
                         '上游模板': str(profile[0].relative_to(args.source)) if profile else None,
                         '模板摘要': hashlib.sha256(profile[0].read_bytes()).hexdigest() if profile else None,
                         '含预设天赋': bool(talents)})
    (args.output / 'profiles.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'生成 {len(rows)} 个专精的两组配置，其中 {sum(r["含预设天赋"] for r in rows)} 个专精有上游天赋模板。')


if __name__ == '__main__':
    main()
