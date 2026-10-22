"""审查早期未编号的翻译缓存，失效旧机器译文并保留原记录供核对。"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_icy_full_bundle import FileTranslator, dump, field_values, fingerprint, load_terms


def numeric_review(folder):
    markup = re.compile(r'<pre\b[^>]*>.*?</pre>|<code\b[^>]*>.*?</code>|<[^>]+>|\[\[.*?\]\]', re.S)
    translator = FileTranslator(folder, load_terms(folder))
    relevant = set()
    for path in (folder / 'converted').glob('*.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        for value in field_values(record):
            relevant.add(value)
            relevant.update(translator.segments(value))

    def normalize(value):
        value = value.replace(',', '')
        return str(int(value)) if value.isdigit() else value

    def chinese(value):
        digits = dict(zip('零一二三四五六七八九', range(10)))
        digits['两'] = 2
        units = {'十': 10, '百': 100, '千': 1000, '万': 10000}
        number = section = total = 0
        for char in value:
            if char in digits:
                number = digits[char]
            elif units[char] < 10000:
                section += (number or 1) * units[char]
                number = 0
            else:
                total += (section + number or 1) * units[char]
                section = number = 0
        return str(total + section + number)

    flags, checked = [], 0
    for path in (folder / 'translation-cache').glob('*.json'):
        row = json.loads(path.read_text(encoding='utf-8'))
        if row['source'] not in relevant or row.get('invalid_reason') or len(row['source']) > 4000:
            continue
        source, translated = markup.sub('', row['source']), markup.sub('', row['translated'])
        before = Counter(normalize(value) for value in re.findall(r'\d+(?:[.,]\d+)*', source))
        after = Counter(normalize(value) for value in re.findall(r'\d+(?:[.,]\d+)*', translated))
        after.update(chinese(value) for value in re.findall(r'[零一二两三四五六七八九十百千万]+', translated))
        after['2'] += translated.count('双')
        after['1'] += len(re.findall(r'单[个体手]', translated))
        if '1-shot' in source and '秒杀' in translated:
            after['1'] += 1
        checked += 1
        if before - after:
            flags.append({'source': row['source'], 'translated': row['translated'],
                          'missing_numeric_forms': dict(before - after), 'cache_file': str(path)})
    report = {'checked': checked, 'flag_count': len(flags), 'flags': flags,
              'scope': '当前转换记录的正文、标题、引用名及其文本节点，不含已废弃的历史缓存块',
              '说明': '数字形式差异仅为核对线索；需区分正确的中文数字、原文日期黏连与真实漏译。'}
    dump(folder / 'numeric-review.json', report)
    print(json.dumps({'已核对缓存': checked, '待人工核对数字形式': len(flags)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--invalidate-legacy', action='store_true')
    parser.add_argument('--numbers-only', action='store_true')
    args = parser.parse_args()
    folder = args.work_dir
    if args.numbers_only:
        numeric_review(folder)
        return
    batch_files, unindexed_sources = [], set()
    for path in (folder / 'translation-batches').glob('*.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        if '"id"' in (value.get('answer') or ''):
            batch_files.append(path)
        else:
            unindexed_sources.update(value.get('sources', []))
    if not batch_files:
        raise ValueError('尚无独立编号的翻译批次')
    cutoff = min(path.stat().st_mtime for path in batch_files)
    report_path = folder / 'legacy-cache-review.json'
    previous = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    legacy = set(previous.get('legacy_sources', [])) | unindexed_sources
    translator = FileTranslator(folder, load_terms())
    for path in (folder / 'translation-cache').glob('*.json'):
        if path.stat().st_mtime >= cutoff:
            continue
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('schema') != 'indexed-v2' and translator.should_translate(value['source']):
            legacy.add(value['source'])
    affected = set(legacy)
    for path in (folder / 'converted').glob('*.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        for value in field_values(record):
            if value in legacy or any(unit in legacy for unit in translator.segments(value)):
                affected.add(value)
    invalidated = 0
    if args.invalidate_legacy:
        for source in affected:
            path = folder / 'translation-cache' / (fingerprint('icy-full-v1\n' + source) + '.json')
            if not path.exists():
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('schema') == 'indexed-v2':
                continue
            value['invalid_reason'] = '早期批次未使用独立编号，重新完整翻译后才能进入最终包'
            dump(path, value)
            invalidated += 1
    report = {'cutoff': cutoff, 'legacy_sources': sorted(legacy), 'affected_count': len(affected),
              'invalidated_count': invalidated, '说明': '只失效待重译的机器译文，保留全部缓存文件；官方术语和确定性替换继续复用。'}
    dump(report_path, report)
    print(json.dumps({key: value for key, value in report.items() if key != 'legacy_sources'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
