"""采集 Icy Veins 专精目录，生成兼容现有导入命令的中文导读包。"""

import argparse
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_IDENTITY_MAP

ENTRY = 'https://www.icy-veins.com/wow/death-knight-guide'
CHAPTERS = {
    'Overview': '专精概览', 'Leveling': '练级', 'Easy Mode': '快速入门',
    'Talents': '天赋与构筑', 'Rotation': '技能循环与冷却', 'Gear': '装备选择',
    'Stats': '属性优先级', 'Enchants': '附魔与消耗品', 'Mythic+': '大秘境',
    'Macros': '宏与插件', 'Simulations': '模拟', 'Spell List': '技能列表',
    'How to Improve': '进阶与常见问题', 'FAQ': '常见问题',
}


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def slug_part(value):
    return re.sub(r'(?<=[a-z])(?=[A-Z])', '-', value).lower()


def fetch(url):
    with requests.Session() as session:
        session.headers['User-Agent'] = 'LMonitor-Guide-Directory/1.0'
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
        response = session.get(url, timeout=(10, 45))
        response.raise_for_status()
        if urlparse(response.url).hostname != 'www.icy-veins.com':
            raise ValueError('来源跳转到了其他站点：' + response.url)
        return BeautifulSoup(response.content, 'html.parser')


def discover(soup):
    links = {urljoin(ENTRY, a['href']) for a in soup.select('a[href]')}
    result = []
    for spec_id, (cls, spec) in SPEC_IDENTITY_MAP.items():
        prefix = f'https://www.icy-veins.com/wow/{slug_part(spec)}-{slug_part(cls)}-pve-'
        matches = sorted(url for url in links if re.fullmatch(re.escape(prefix) + r'(dps|tank|healing)-guide', url))
        if len(matches) != 1:
            raise ValueError(f'专精 {spec_id} 的入口缺失或重复：{matches}')
        result.append(dict(spec_id=spec_id, class_name=cls, spec_name=spec, source_url=matches[0]))
    return result


def collect_one(identity):
    url = identity['source_url']
    soup = fetch(url)
    title_node = soup.select_one('h1')
    title = title_node.get_text(' ', strip=True) if title_node else ''
    version = re.search(r'\b\d+\.\d+(?:\.\d+)?\b', title)
    if not version:
        raise ValueError('无法确认页面版本：' + url)
    metadata = None
    for script in soup.find_all('script', type='application/ld+json'):
        value = json.loads(script.get_text())
        if isinstance(value, dict) and value.get('@type') == 'Article':
            metadata = value
            break
    if not metadata or metadata.get('url') != url:
        raise ValueError('文章来源身份不匹配：' + url)
    author = metadata.get('author', {})
    if isinstance(author, list):
        author = {'name': '、'.join(row['name'] for row in author)}
    chapters = []
    seen = set()
    prefix = url.rsplit('-', 1)[0] + '-'
    spec_prefix = f'https://www.icy-veins.com/wow/{slug_part(identity["spec_name"])}-{slug_part(identity["class_name"])}-'
    menus = [menu for menu in soup.select('.guide-header__selectors .dropdown__menu')
             if any(a.get_text(' ', strip=True) == 'Overview' for a in menu.select('a[href]'))]
    if len(menus) != 1:
        raise ValueError('无法确定专精章节菜单：' + url)
    for link in menus[0].select('a[href]'):
        label = link.get_text(' ', strip=True)
        href = urljoin(url, link['href'])
        if not (href.startswith(prefix) or (label == 'Leveling' and href.startswith(spec_prefix))):
            continue
        if label not in CHAPTERS:
            raise ValueError(f'发现未映射章节：{label}，{url}')
        if href not in seen:
            chapters.append({'title': CHAPTERS[label], 'url': href})
            seen.add(href)
    if not {'专精概览', '天赋与构筑', '技能循环与冷却', '装备选择'}.issubset({c['title'] for c in chapters}):
        raise ValueError('核心章节链接不完整：' + url)
    overview = soup.select_one('.guide-page-content h2')
    paragraph = overview.find_next('p') if overview else None
    # 概览只输出供人工核对，文件仅保存作者、版本、目录等元数据。
    excerpt = paragraph.get_text(' ', strip=True)[:650] if paragraph else ''
    print(f'{identity["spec_id"]} {title}\n{excerpt}\n', flush=True)
    return {**identity, 'game_version': version[0], 'author': author.get('name', ''),
            'source_modified': metadata.get('dateModified', ''), 'chapters': chapters}


def make_draft(source, summary, collected_at):
    cls, spec = source['class_name'], source['spec_name']
    label = f'{CLASS_CN[cls]}·{SPEC_CN[spec]}'
    markdown = f'## 专精导读\n\n{summary}\n\n'
    markdown += '本页提供中文简要导读与原文导航；具体天赋、装备和技能优先级请查看对应章节。\n\n'
    markdown += '## 原文章节\n\n'
    markdown += '\n'.join(f'- [{row["title"]}](<{row["url"]}>)' for row in source['chapters'])
    markdown += f'\n\n## 来源信息\n\n- 来源：[Icy Veins](<{source["source_url"]}>)\n'
    markdown += f'- 原文作者：{source["author"]}\n- 页面版本：{source["game_version"]}\n'
    markdown += f'- 原文更新：{source["source_modified"]}\n- 采集时间：{collected_at}\n'
    payload = {**source, 'content_kind': '中文导读与原文导航', 'collected_at': collected_at}
    return {**source, 'slug': 'icy-' + urlparse(source['source_url']).path.rsplit('/', 1)[1],
            'title': label + '攻略导读（Icy Veins）', 'guide_type': 'general',
            'tags': ['icy', source['game_version'], '通用', '导读'], 'is_visible': True,
            'source_author_profile': {'name': source['author'], 'title': '原文作者'},
            'author_profile': {'name': '站内编辑', 'title': '中文导读整理'},
            'content_markdown': markdown, 'source_markdown': markdown,
            'source_payload': payload, 'source_hash': digest(encode(payload)), 'check_data': {}}


def build_bundle(index, summaries, destination):
    records = index['records']
    identities = [row['spec_id'] for row in records]
    if len(identities) != len(set(identities)) or set(identities) != set(SPEC_IDENTITY_MAP):
        raise ValueError('目录必须完整覆盖所有专精且不能重复')
    if set(summaries) != {str(key) for key in identities}:
        raise ValueError('中文导读必须与专精目录逐项对应')
    drafts = []
    for row in records:
        if (row['class_name'], row['spec_name']) != SPEC_IDENTITY_MAP[row['spec_id']]:
            raise ValueError('目录专精身份错误')
        summary = summaries[str(row['spec_id'])]
        if not isinstance(summary, str) or not 10 <= len(summary) <= 180:
            raise ValueError('每篇导读应为 10 至 180 字的中文简要说明')
        drafts.append(make_draft(row, summary, index['collected_at']))
    manifest = {'created_at': index['collected_at'], 'entry_url': ENTRY,
                'content_kind': '中文导读与原文导航，非全文转载', 'total': len(drafts),
                'terms_sha256': digest('[]'), 'records': []}
    files = {'terms.json': '[]', 'source-index.json': encode(index)}
    for draft in drafts:
        entry = {key: draft[key] for key in ('slug', 'game_version', 'spec_id', 'is_visible', 'tags')}
        entry['markdown_sha256'] = digest(draft['content_markdown'])
        entry['tags_sha256'] = digest(json.dumps(draft['tags'], ensure_ascii=False))
        entry['author_profiles_sha256'] = digest(json.dumps(
            [draft['source_author_profile'], draft['author_profile']], ensure_ascii=False, sort_keys=True))
        manifest['records'].append(entry)
        stem = draft['slug'] + '-' + draft['game_version']
        files[stem + '.json'] = encode(draft)
        files[stem + '.md'] = draft['content_markdown']
    files['manifest.json'] = encode(manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建，避免覆盖已经交付的初始化包。
    with ZipFile(destination, 'x', compression=ZIP_DEFLATED) as bundle:
        for name, value in files.items():
            bundle.writestr(name, value)
    print(f'已生成 {len(drafts)} 篇导读：{destination}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect', type=Path, help='在线采集目录元数据，保存为新文件')
    parser.add_argument('--source-index', type=Path, help='使用已采集的目录，离线生成导读包')
    parser.add_argument('--summaries', type=Path, help='按专精编号保存的中文简要导读')
    parser.add_argument('--output', type=Path, help='输出新的 ZIP 文件路径')
    args = parser.parse_args()
    if args.collect:
        if args.collect.exists():
            parser.error('采集输出文件已存在，请指定新路径')
        identities = discover(fetch(ENTRY))
        with ThreadPoolExecutor(max_workers=3) as pool:
            records = list(pool.map(collect_one, identities))
        index = {'entry_url': ENTRY, 'collected_at': datetime.now(timezone.utc).isoformat(), 'records': records}
        args.collect.parent.mkdir(parents=True, exist_ok=True)
        with args.collect.open('x', encoding='utf-8') as stream:
            stream.write(encode(index))
        print(f'已采集 {len(records)} 个专精，{sum(len(r["chapters"]) for r in records)} 条章节导航')
    elif args.source_index and args.summaries and args.output:
        build_bundle(json.loads(args.source_index.read_text(encoding='utf-8')),
                     json.loads(args.summaries.read_text(encoding='utf-8')), args.output)
    else:
        parser.error('请使用 --collect，或同时指定 --source-index、--summaries、--output')


if __name__ == '__main__':
    main()
