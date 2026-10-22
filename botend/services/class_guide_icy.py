"""将本地提供的 Icy 页面正文转换为现有攻略块，保留交互组件的全部方案。"""

import copy
import hashlib
import html
import json
import re
from collections import Counter
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from botend.constants.wow import SPEC_IDENTITY_MAP

from botend.services.class_guide_content import clean_html, safe_url, validate_blocks, walk_blocks
from botend.services.class_guide_markdown import blocks_to_markdown, compile_markdown
from botend.services.class_guide_macros import localize_macro, macro_name_map, macro_source_repairs

CHROME = ('.table-of-contents,.content-toc,.cta-block__wrapper-new,.inline-author-block,'
          '.internal-links,.back-to-top,.spec_class_container')
REFERENCE = re.compile(r'^(wow\w*)/(spell|item|currency|affix|achievement|npc)/(\d+)(?:\?(.*))?$')
CONDITION = re.compile(r'^rotation_line_(.+)_(on|off)$')
LEVEL_RANGE = re.compile(r'^[A-Za-z0-9_-]+_level_(\d+)(?:_(\d+))?$')
MACRO_LINE = re.compile(r'^(?:#show(?:tooltip|icon)?\b|/[A-Za-z]+\b)', re.I)


def is_game_talent_code(value):
    if not re.fullmatch(r'[A-Za-z0-9+/]{40,}={0,2}', value):
        return False
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    header = sum(alphabet.index(char) << (6 * index) for index, char in enumerate(value[:4]))
    return (header & 255) in {1, 2} and (header >> 8) in SPEC_IDENTITY_MAP


def encode_item_variant(bonuses, level=None, item_level=None):
    """使用当前低位优先引用协议，避免新增另一套物品变体解析流程。"""
    if len(bonuses) > 15 or any(not 0 <= value <= 65535 for value in bonuses):
        raise ValueError('物品奖励编号超过现有引用协议范围')
    if level is not None and not 0 <= level <= 255:
        raise ValueError('角色等级超过现有引用协议范围')
    if item_level is not None and not 0 <= item_level <= 65535:
        raise ValueError('物品等级超过现有引用协议范围')
    value, count = 0, 0

    def put(number, width):
        nonlocal value, count
        value |= number << count
        count += width

    flags = len(bonuses) << 11
    flags |= (1 << 10) if level is not None else 0
    flags |= (1 << 16) if item_level is not None else 0
    put(9, 8)
    put(flags, 32)
    if level is not None:
        put(level, 8)
    for bonus in bonuses:
        put(bonus, 16)
    if item_level is not None:
        put(item_level, 16)
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    return ''.join(alphabet[(value >> offset) & 63] for offset in range(0, count, 6))


class IcyPageConverter:
    def __init__(self, source, source_url, talent_exports=None):
        parsed = urlparse(source_url)
        if parsed.scheme != 'https' or parsed.hostname not in {'www.icy-veins.com', 'icy-veins.com'} or not parsed.path.startswith('/wow/'):
            raise ValueError('页面来源必须为 Icy Veins 魔兽页面')
        self.soup = BeautifulSoup(source, 'html.parser', preserve_whitespace_tags={'pre', 'code'})
        self.body = self.soup.select_one('.guide-page-content')
        if self.body is None:
            raise ValueError('页面缺少攻略正文容器')
        self.url = source_url
        self.refs = {}
        self.issues = []
        self.conditions = []
        self.sequence = 0
        self.labels = {}
        self.tree_codes = {}
        self.talent_exports = talent_exports or {}

    def block(self, kind, **fields):
        self.sequence += 1
        return {'id': f'icy-{self.sequence}', 'type': kind, 'children': [], **fields}

    def inline(self, value):
        return clean_html(value)

    def prepare(self):
        for comment in list(self.body.find_all(string=lambda value: isinstance(value, Comment))):
            comment.extract()
        for element in self.body.select('.rotation_switch[id],.rotation_presets[id]'):
            key = element['id'].removeprefix('rotation_switch_')
            header = element.select_one('.rotation_presets_header')
            self.labels[key] = (header or element).get_text(' ', strip=True)
        for script in self.body.find_all('script'):
            text = script.get_text()
            if 'MidnightTalentCalculator' in text:
                strings = re.findall(r'''["']([^"']+)["']''', text)
                target = next((s for s in strings if s.startswith('midnight-skill-builder-')), None)
                code = next((s for s in strings if s != target and re.fullmatch(r'[A-Za-z0-9+#/=_:*\-]{20,}', s)), None)
                if target and code:
                    self.tree_codes[target] = code
        for node in list(self.body.select(CHROME)):
            node.decompose()
        for reference_index, node in enumerate(list(self.body.select('[data-iv-tooltip],[data-wowhead]')), 1):
            if node.parent is None or node.attrs is None:
                continue
            raw = html.unescape(node.get('data-iv-tooltip', ''))
            match = REFERENCE.fullmatch(raw)
            if match:
                branch, kind, object_id, query = match.groups()
                params = parse_qs(query or '')
            else:
                params = parse_qs(html.unescape(node.get('data-wowhead', '')))
                kind = next((key for key in ('spell', 'item') if params.get(key)), None)
                object_id = params.get(kind, [''])[0]
                branch = 'wow'
            if kind is None or not str(object_id).isdigit():
                self.issues.append({'type': '引用格式未知', 'value': raw})
                continue
            if kind in {'currency', 'affix', 'achievement', 'npc'}:
                category = 'mythic-affix' if kind == 'affix' else kind
                anchor = self.soup.new_tag('a', href=f'https://www.wowhead.com/{category}=' + object_id)
                anchor.string = node.get_text(' ', strip=True)
                node.replace_with(anchor)
                continue
            suffix = ''
            if kind == 'item' and any(key in params for key in ('bonus', 'level', 'ilvl')):
                try:
                    bonuses = [int(value) for value in params.get('bonus', [''])[0].split(':') if value]
                    level = int(params['level'][0]) if 'level' in params else None
                    item_level = int(params['ilvl'][0]) if 'ilvl' in params else None
                    suffix = '@' + encode_item_variant(bonuses, level, item_level)
                except (ValueError, TypeError) as exc:
                    self.issues.append({'type': '物品变体无法解析', 'value': raw, 'reason': str(exc)})
            token = f'[[{kind}:{object_id}{suffix}]]'
            name = node.get_text(' ', strip=True)
            wrapper = node.find_parent(class_='spell_icon_span')
            target = wrapper if wrapper and len(wrapper.select('[data-iv-tooltip],[data-wowhead]')) == 1 else node
            record = {'kind': kind, 'id': int(object_id), 'variant': suffix.removeprefix('@'),
                      'source_name': name, 'source_branch': branch, 'source_parameters': params}
            existing = self.refs.setdefault(token, record)
            origin = {'branch': branch, 'parameters': params}
            origins = existing.setdefault('source_contexts', [])
            if origin not in origins:
                origins.append(origin)
            existing.setdefault('source_occurrences', []).append({
                'source_url': self.url, 'reference_index': reference_index,
                'raw': node.get('data-iv-tooltip') or node.get('data-wowhead', ''),
                'parameters': params})
            target.replace_with(NavigableString(token))
        for element in self.body.select('[href],[src]'):
            for field in ('href', 'src'):
                if element.get(field):
                    element[field] = safe_url(urljoin(self.url, element[field]), image=field == 'src')
        # 大秘境章节也会把可执行宏写成普通列表或段落，先标为代码再翻译。
        for element in list(self.body.select('ul,ol,p')):
            if element.parent is None or element.find_parent(['pre', 'code']):
                continue
            lines = ([li.get_text(' ', strip=True) for li in element.find_all('li', recursive=False)]
                     if element.name in {'ul', 'ol'} else element.get_text().strip().splitlines())
            text = element.get_text().strip()
            if element.name == 'p' and is_game_talent_code(text):
                wrapper = self.soup.new_tag('div', attrs={'class': 'export-string-wrapper'})
                code = self.soup.new_tag('span', attrs={'class': 'export-string__code'})
                code.string = text
                wrapper.append(code)
                element.replace_with(wrapper)
                continue
            if not lines or not all(MACRO_LINE.match(line.strip()) for line in lines):
                continue
            pre, code = self.soup.new_tag('pre'), self.soup.new_tag('code')
            code.string = '\n'.join(lines)
            pre.append(code)
            element.replace_with(pre)
        for node in self.body.find_all(True):
            for class_name in node.get('class', []):
                level = LEVEL_RANGE.fullmatch(class_name)
                if not level:
                    continue
                low, high = int(level[1]), int(level[2]) if level[2] else None
                self.conditions.append({'classes': [class_name], 'level_min': low, 'level_max': high})
                label = f'等级 {low}–{high}：' if high is not None else f'等级 {low} 起：'
                strong = self.soup.new_tag('strong')
                strong.string = label
                node.insert(0, strong)
            matches = [CONDITION.fullmatch(c) for c in node.get('class', [])]
            terms = [match for match in matches if match]
            if not terms:
                continue
            labels = []
            for match in terms:
                key, state = match.groups()
                name = self.labels.get(key)
                if not name and key == 'moh' and 'Master of Harmony' in self.body.get_text(' ', strip=True):
                    name = 'Master of Harmony'
                if not name:
                    self.issues.append({'type': '条件名称缺失', 'value': key})
                    name = key
                labels.append(name + ('（启用）' if state == 'on' else '（未启用）'))
            self.conditions.append({'classes': [m[0] for m in terms], 'labels': labels})
            strong = self.soup.new_tag('strong')
            strong.string = '适用条件：' + '；'.join(labels) + '。 '
            node.insert(0, strong)

    def rows(self, parent):
        result = []
        for node in list(parent.children):
            result.extend(self.visit(node))
        return result

    def visit(self, node):
        if isinstance(node, Comment):
            return []
        if isinstance(node, NavigableString):
            return [self.block('html', html=html.escape(str(node)))] if str(node).strip() else []
        if not isinstance(node, Tag):
            return []
        if node.name in {'script', 'style', 'button', 'input', 'svg', 'noscript'}:
            return []
        if node.name in {'iframe', 'object', 'embed', 'video', 'canvas'}:
            urls = [node.get('src', '')] + [source.get('src', '') for source in node.select('source[src]')]
            urls = [url for url in urls if safe_url(url)]
            if urls:
                label = '观看原文视频' if node.name == 'video' or any('youtube' in url for url in urls) else '查看原文交互内容'
                return [self.block('html', html='<p><a href="' + html.escape(url, quote=True) + '">' + label + '</a></p>') for url in urls]
            self.issues.append({'type': '动态内容需要补充', 'tag': node.name, 'url': node.get('src', '')})
            return []
        classes = node.get('class', [])
        if 'changelog_wrapper' in classes:
            return [self.block('details', title='历史更新记录', children=self.rows(node))]
        if 'leveling-slider-wrapper' in classes:
            return [self.block('callout', title='练级内容阅读说明',
                               html='<p>原文等级滑块中的内容已按等级区间展开，请阅读对应等级的说明。</p>')]
        if 'heading_number' in classes or 'image_block_header_buttons' in classes:
            return []
        if re.fullmatch(r'h[1-6]', node.name):
            return [self.block('heading', title=self.inline(node.decode_contents()), data={'level': min(6, int(node.name[1]) + 1)})]
        if 'export-string-wrapper' in classes or 'export-string' in classes:
            title = node.select_one('.export-string__title')
            code = node.select_one('.export-string__code,.export-string__code_large,.export-string__code_large_macro')
            if code is None:
                self.issues.append({'type': '导出组件缺少代码'})
                return self.rows(node)
            value = code.get_text().strip()
            label = self.inline(title.decode_contents()) if title else '导入代码'
            if re.fullmatch(r'[A-Za-z0-9+/]{20,}={0,2}', value):
                block = self.block('talents', title=label, data={'code': value})
            else:
                block = self.block('code', title=label, data={'code': value})
            code.decompose()
            for element in node.select('summary,button,.export-string__title'):
                element.decompose()
            for element in node.select('details.export-string'):
                element.unwrap()
            prefix = [self.block('heading', title=label, data={'level': 4})] if block['type'] == 'code' else []
            return [*prefix, block, *self.rows(node)]
        if 'midnight-skill-builder-embed' in classes:
            value = self.tree_codes.get(node.get('id'))
            if not value:
                self.issues.append({'type': '计算器缺少静态构筑代码', 'id': node.get('id')})
                return []
            if re.fullmatch(r'[A-Za-z0-9+/]{20,}={0,2}', value):
                return [self.block('talents', title='天赋构筑', data={'code': value})]
            exported = self.talent_exports.get(value.strip())
            if exported:
                return [self.block('talents', title='天赋构筑', data={'code': exported['code']}),
                        self.block('details', title='来源计算器编码', children=[
                            self.block('html', html='<pre><code>' + html.escape(value) + '</code></pre>')])]
            # 网站计算器的紧凑编码与游戏导入串不同，不能误接到本站天赋导入。
            return [self.block('html', html='<p>来源天赋计算器编码</p><pre><code>' + html.escape(value) + '</code></pre>')]
        if node.name == 'details':
            summary = node.find('summary', recursive=False)
            title = self.inline(summary.decode_contents()) if summary else '展开说明'
            if summary:
                summary.extract()
            return [self.block('details', title=title, children=self.rows(node))]
        if node.name == 'fieldset':
            legend = node.find('legend', recursive=False)
            title = self.inline(legend.decode_contents()) if legend else '补充说明'
            if legend:
                legend.extract()
            return [self.block('details', title=title, children=self.rows(node))]
        if node.name == 'pre' and MACRO_LINE.match(node.get_text().lstrip()):
            return [self.block('code', data={'code': node.get_text().strip('\n')})]
        if 'bis_items_grid' in classes:
            rows = []
            for item in node.select('.bis_item'):
                slot = item.select_one('.bis_item_slot')
                label = self.inline(slot.decode_contents()) if slot else '未标注部位'
                if slot:
                    slot.extract()
                rows.append('<tr><th>' + label + '</th><td>' + self.inline(item.decode_contents()) + '</td></tr>')
            if not rows:
                self.issues.append({'type': '装备网格缺少条目'})
            return [self.block('html', html='<table><thead><tr><th>部位</th><th>装备与获取说明</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')]
        if any(c in classes for c in ('image_block_content', 'rotation_block_content', 'filter-content-wrapper', 'talent-calculator-wrapper')):
            target_id = node.get('id', '')
            button = self.soup.find(id=target_id + '_button') if target_id else None
            title = self.inline(button.decode_contents()) if button else ''
            if not title and node.get('data-filter-index'):
                title = '天赋方案 ' + node['data-filter-index']
            prefix = [self.block('heading', title=title, data={'level': 3})] if title else []
            return prefix + self.rows(node)
        if node.name in {'p', 'ul', 'ol', 'table', 'pre', 'blockquote', 'figure', 'hr',
                         'img', 'a', 'strong', 'b', 'em', 'i', 'u', 's', 'mark', 'span', 'code'}:
            # 嵌套表格及有序列表整体保留，避免拆开后丢失行列及步骤关系。
            return [self.block('html', html=self.inline(str(node)))]
        return self.rows(node)

    def convert(self):
        self.prepare()
        blocks = validate_blocks(self.rows(self.body))
        markdown = blocks_to_markdown(blocks)
        compiled = compile_markdown(markdown)
        counts = Counter(block['type'] for block in walk_blocks(compiled))
        if not compiled:
            raise ValueError('页面正文为空')
        return {'source_url': self.url, 'blocks': compiled, 'content_markdown': markdown,
                'source_refs': self.refs, 'conditions': self.conditions,
                'issues': self.issues, 'block_counts': dict(counts)}


def convert_page(source, source_url, talent_exports=None):
    """处理调用方提供的 HTML；此模块不负责网络下载或自动翻译。"""
    return IcyPageConverter(source, source_url, talent_exports).convert()


def localize_macro_blocks(blocks, terms, source_refs=None):
    """复用当前宏本地化规则，不让翻译服务改写命令和 Lua。"""
    result = copy.deepcopy(blocks)
    names = macro_name_map((row['kind'], row['name_en'], row['name_zh']) for row in terms)
    if source_refs:
        identities = {(ref['kind'], ref['id']) for ref in source_refs.values()}
        names.update(macro_name_map((row['kind'], row['name_en'], row['name_zh']) for row in terms
                                    if (row['kind'], row['object_id']) in identities))
    for key, value in list(names.items()):
        for suffix in ('(racial)', '(summon)', '(teleport)'):
            names.setdefault(key + suffix, value)
    notes = []
    for block in walk_blocks(result):
        if block['type'] == 'code' and block.get('data', {}).get('code'):
            source = block['data']['code']
            block['data']['code'], missing = localize_macro(source, names)
            if missing or macro_source_repairs(source):
                notes.append({'block_id': block['id'], 'missing_names': missing,
                              'source_repairs': macro_source_repairs(source)})
        if '<pre' not in block.get('html', ''):
            continue
        soup = BeautifulSoup(block['html'], 'html.parser', preserve_whitespace_tags={'pre', 'code'})
        changed = False
        for pre in soup.find_all('pre'):
            source = pre.get_text()
            if not re.search(r'(?m)^\s*(?:#show|/(?:cast|use|target|equip|cancel))', source):
                continue
            translated, missing = localize_macro(source, names)
            target = pre.find('code') or pre
            target.clear()
            target.append(NavigableString(translated))
            changed = True
            if missing or macro_source_repairs(source):
                notes.append({'block_id': block['id'], 'missing_names': missing,
                              'source_repairs': macro_source_repairs(source)})
        if changed:
            block['html'] = str(soup)
    return result, notes


def merge_chapters(chapters, talent_exports=None):
    """每专精合并成一篇正文，给翻译服务提供稳定编号的完整块。"""
    chunks, sources, refs, issues = [], [], {}, []
    seen = set()
    for chapter in chapters:
        url, title = chapter['source_url'], chapter['title']
        if url in seen:
            raise ValueError('存在重复章节：' + url)
        seen.add(url)
        result = convert_page(chapter['html'], url, talent_exports)
        chunks.append('## ' + title.replace('\n', ' ') + '\n\n' + result['content_markdown'])
        sources.append({'title': title, 'source_url': url,
                        'source_sha256': hashlib.sha256(chapter['html'].encode()).hexdigest(),
                        'block_counts': result['block_counts'], 'conditions': result['conditions']})
        for token, reference in result['source_refs'].items():
            if token not in refs:
                refs[token] = reference
            else:
                for context in reference.get('source_contexts', []):
                    if context not in refs[token]['source_contexts']:
                        refs[token]['source_contexts'].append(context)
                refs[token].setdefault('source_occurrences', []).extend(reference.get('source_occurrences', []))
        issues.extend({'source_url': url, **entry} for entry in result['issues'])
    if not chunks:
        raise ValueError('至少需要一个章节')
    markdown = '\n\n'.join(chunks)
    blocks = compile_markdown(markdown)
    return {'content_markdown': markdown, 'blocks': blocks, 'chapters': sources,
            'check_data': {'source_refs': refs, 'unsupported': issues},
            'source_hash': hashlib.sha256(json.dumps(sources, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
