"""逐份审计 APL 的所有表达式选项；语法覆盖与中文资料身份覆盖分别统计。"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()

from botend.services.simc_apl.condition_audit import audit_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=ROOT / 'simc-source/ActionPriorityLists/default')
    parser.add_argument('--apl', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fail-on-unknown', action='store_true')
    args = parser.parse_args()
    paths = sorted(set(args.source_dir.glob('*.simc')) | set(args.apl))
    if not paths:
        parser.error('未找到 APL，不能用空集合通过审计')
    reports = [audit_source(path.read_text(encoding='utf-8-sig'), str(path)) for path in paths]
    unknown = Counter(x['token'] for report in reports for x in report['unknown_fields'])
    summary = {'files': len(reports), 'expressions': sum(r['expressions'] for r in reports),
               'unknown_expressions': sum(r['unknown_expressions'] for r in reports),
               'parse_issues': sum(r['parse_issues'] for r in reports),
               'unknown_tokens': dict(unknown.most_common()),
               'boundary': '引用为语法审计构造，不代表真实中文技能、装备和召唤物身份覆盖；所有表达式选项均纳入统计。'}
    args.output.write_text(json.dumps({'summary': summary, 'files': reports}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(args.fail_on_unknown and bool(summary['unknown_expressions'] or summary['parse_issues']))


if __name__ == '__main__':
    raise SystemExit(main())
