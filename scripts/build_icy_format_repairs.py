"""用本地来源 HTML 和完整包生成可校验的旧稿排版修复；不抓取、不改数据库。"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from bs4 import BeautifulSoup
from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_markdown import compile_markdown
from botend.services.class_guide_icy_format import PERFORMANCE_LABELS, stat_widget_data, legacy_inline_groups


def build(bundle, raw_dir):
    result = {'description': '依据 2026-10-22 原始 HTML 恢复属性关系与表现分组，正文哈希不匹配时不应用。', 'articles': {}}
    totals = defaultdict(int)
    with zipfile.ZipFile(bundle) as archive:
        records = json.loads(archive.read('manifest.json'))['records']
        for record in records:
            article = json.loads(archive.read(record['slug'] + '-' + record['game_version'] + '.json'))
            source = list(walk_blocks(compile_markdown(article['source_markdown'])))
            chinese = list(walk_blocks(compile_markdown(article['content_markdown'])))
            if chinese[0].get('title') == '来源与翻译说明':
                chinese = chinese[2:]
            if [b['type'] for b in source] != [b['type'] for b in chinese]:
                raise ValueError('原文与译文块结构不对应：' + record['slug'])
            paragraphs = defaultdict(list)
            chapter = ''
            for left, right in zip(source, chinese):
                if left['type'] == 'heading' and left.get('data', {}).get('level') == 2:
                    chapter = left['title']
                if left['type'] != 'html':
                    continue
                a = BeautifulSoup(left.get('html', ''), 'html.parser').find_all('p')
                b = BeautifulSoup(right.get('html', ''), 'html.parser').find_all('p')
                for index, en in enumerate(a):
                    cn = b[index].get_text(' ', strip=True) if len(a) == len(b) else None
                    paragraphs[chapter].append({'english': en.get_text(' ', strip=True), 'chinese': cn,
                                                'block': right['id'], 'paragraph': index})
            operations, evidence = defaultdict(list), []
            for chapter in article['source_payload']['chapters']:
                url = chapter.get('url') or chapter['source_url']
                path = raw_dir / (urlparse(url).path.rsplit('/', 1)[-1] + '.html')
                raw = path.read_bytes()
                soup = BeautifulSoup(raw, 'html.parser')
                rows = paragraphs[chapter['title']]
                cursor = 0
                widgets = soup.select('.guide-page-content .stat-priority-widget')
                for widget in widgets:
                    data = stat_widget_data(widget)
                    labels = data['stat_labels']
                    match = next((i for i in range(cursor, len(rows) - len(labels) + 1)
                                  if [r['english'] for r in rows[i:i+len(labels)]] == labels), None)
                    if match is None:
                        raise ValueError('属性序列未匹配：' + url + str(labels))
                    matched = rows[match:match+len(labels)]
                    assert all(r['chinese'] is not None for r in matched), '属性段落数不对应：' + url
                    assert len({r['block'] for r in matched}) == 1
                    assert [r['paragraph'] for r in matched] == list(range(matched[0]['paragraph'], matched[0]['paragraph'] + len(labels)))
                    operations[matched[0]['block']].append({'kind': 'priority', 'paragraph': matched[0]['paragraph'],
                        'text': [r['chinese'] for r in matched], 'relation': data['relation']})
                    cursor = match + len(labels)
                    totals['属性组件'] += 1
                cursor = 0
                panels = soup.select('.guide-page-content .performance-overview__panel')
                for panel in panels:
                    header = panel.select_one('.performance-header, .performance-overview__panel-body > .centered')
                    label = soup.find(id=panel.get('aria-labelledby', ''))
                    if not header or not label:
                        raise ValueError('表现组件缺少明确标题：' + url)
                    text = header.get_text(' ', strip=True)
                    match = next((i for i in range(cursor, len(rows)) if rows[i]['english'] == text), None)
                    if match is None:
                        raise ValueError('表现标题未匹配：' + url + text)
                    row = rows[match]
                    assert row['chinese'] is not None, '表现段落数不对应：' + url
                    operations[row['block']].append({'kind': 'performance', 'paragraph': row['paragraph'],
                        'text': [row['chinese']], 'label': PERFORMANCE_LABELS[label.get_text(' ', strip=True)], 'rating': panel.get('data-rating', '')})
                    cursor = match + 1
                    totals['表现分组'] += 1
                merged = 0
                for fragments in legacy_inline_groups(soup):
                    matches = [i for i in range(len(rows) - len(fragments) + 1)
                               if [' '.join(r['english'].split()) for r in rows[i:i+len(fragments)]] == fragments]
                    if len(matches) != 1:
                        continue
                    matched = rows[matches[0]:matches[0]+len(fragments)]
                    if any(r['chinese'] is None for r in matched) or len({r['block'] for r in matched}) != 1:
                        continue
                    first = matched[0]
                    positions = {r['paragraph'] for r in matched}
                    if positions != set(range(first['paragraph'], first['paragraph'] + len(matched))):
                        continue
                    occupied = {i for op in operations[first['block']] for i in range(op['paragraph'], op['paragraph'] + len(op['text']))}
                    if positions & occupied:
                        continue
                    operations[first['block']].append({'kind': 'paragraph', 'paragraph': first['paragraph'], 'text': [r['chinese'] for r in matched]})
                    totals['行内断段'] += 1
                    merged += 1
                if widgets or panels or merged:
                    evidence.append({'url': url, 'sha256': hashlib.sha256(raw).hexdigest()})
            result['articles'][record['slug']] = {'content_sha256': hashlib.sha256(article['content_markdown'].encode()).hexdigest(),
                'sources': evidence, 'blocks': dict(operations)}
            print('已核验：' + record['slug'], flush=True)
    result['counts'] = dict(totals)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--raw-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.bundle, args.raw_dir)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result['counts'], ensure_ascii=False))
