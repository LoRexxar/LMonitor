"""读取三类页面的 DOM，去掉第三方正文后验证格式适配并保存统计。"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from bs4 import NavigableString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django

django.setup()

from scripts.build_icy_guide_bundle import fetch
from botend.services.class_guide_icy import convert_page


def check_page(suffix):
    url = 'https://www.icy-veins.com/wow/blood-death-knight-pve-tank-' + suffix
    soup = fetch(url)
    body = soup.select_one('.guide-page-content')
    if body is None:
        raise ValueError('缺少正文容器：' + url)
    tree_codes = []
    for script in list(body.select('script')):
        text = script.get_text()
        if 'MidnightTalentCalculator' in text:
            strings = re.findall(r'''["']([^"']+)["']''', text)
            target = next((value for value in strings if value.startswith('midnight-skill-builder-')), None)
            code = next((value for value in strings if value != target and re.fullmatch(r'[A-Za-z0-9+#/_=-]{20,}', value)), None)
            if target and code:
                tree_codes.append((target, code))
        script.decompose()
    for index, text in enumerate(list(body.find_all(string=True)), 1):
        if not str(text).strip():
            continue
        if text.find_parent(class_=re.compile(r'^export-string__code')):
            # 构筑字符串是结构参数，不是攻略叙述。
            continue
        replacement = '/cast 测试技能' if text.find_parent(['pre', 'code']) else '结构样本' + str(index)
        text.replace_with(NavigableString(replacement))
    for node in body.find_all(True):
        for attr in list(node.attrs):
            if attr.startswith('on'):
                del node[attr]
        for attr in ('alt', 'title', 'aria-label'):
            if attr in node.attrs:
                node[attr] = '结构样例'
    for target, code in tree_codes:
        script = soup.new_tag('script')
        script.string = 'new MidnightTalentCalculator(' + json.dumps(target) + ',' + json.dumps(code) + ',{});'
        body.append(script)
    result = convert_page(str(body), url)
    return {'页面类型': suffix, '页面': url, '内容块': result['block_counts'],
            '引用数': len(result['source_refs']), '条件数': len(result['conditions']), '结构问题': result['issues']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='新的结构统计文件路径')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出文件已存在，请指定新路径')
    report = {'验证方式': '保留真实 DOM，将攻略叙述替换为原创占位文本，仅保存结构统计和问题。',
              '结果': []}
    for suffix in ('spec-builds-talents', 'rotation-cooldowns-abilities', 'gear-best-in-slot'):
        result = check_page(suffix)
        report['结果'].append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
