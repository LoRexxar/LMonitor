"""单篇 Markdown 是攻略的内容源；目录和组件均由正文解析得到。"""

import html
import json
import re

from bs4 import BeautifulSoup, NavigableString
from markdown_it import MarkdownIt

from botend.services.class_guide_codec import decode_component, component_html
from botend.services.class_guide_content import clean_html, validate_blocks
from botend.services.class_guide_sections import expand_tab_sections

EXTENSIONS = {'tabs', 'tab', 'columns', 'column', 'details', 'callout', 'rating', 'changelog',
              'group', 'accordion', 'talents', 'gear', 'rotation', 'priority', 'timeline', 'simulation', 'unsupported'}
COMPONENTS = {'talents', 'gear', 'rotation', 'priority', 'timeline', 'simulation'}
DIRECTIVE = re.compile(r'^:::(\w[\w-]*)(?:\s+(.*))?$')


def html_to_markdown(value):
    soup = BeautifulSoup(clean_html(value), 'html.parser')
    def text(node, depth=0):
        if isinstance(node, NavigableString):
            return str(node)
        name = node.name
        inner = ''.join(text(child, depth) for child in node.children)
        if name in ('strong', 'b'):
            if node.find_parent(['strong', 'b']) or not inner.strip():
                return inner
            return re.match(r'^\s*', inner)[0] + '**' + inner.strip() + '**' + re.search(r'\s*$', inner)[0]
        if name in ('em', 'i'):
            if node.find_parent(['em', 'i']) or not inner.strip():
                return inner
            return re.match(r'^\s*', inner)[0] + '*' + inner.strip() + '*' + re.search(r'\s*$', inner)[0]
        if name == 'a' and node.get('href'):
            return '[{}](<{}>)'.format(inner, node['href'])
        if name == 'img':
            return '![{}](<{}>)'.format(node.get('alt', ''), node.get('src', ''))
        if name == 'br':
            return '  \n'
        if name in ('ul', 'ol'):
            rows = []
            for index, child in enumerate(node.find_all('li', recursive=False), 1):
                prefix = '{}. '.format(index) if name == 'ol' else '- '
                content = ''.join(text(c, depth + 1) for c in child.children).strip()
                lines = content.splitlines() or ['']
                rows.append(prefix + lines[0] + ''.join('\n' + ' ' * len(prefix) + line for line in lines[1:]))
            return '\n\n' + '\n'.join(rows) + '\n\n'
        if name == 'blockquote':
            return '\n\n' + '\n'.join('> ' + line for line in inner.strip().splitlines()) + '\n\n'
        if name == 'pre':
            code = node.get_text().strip('\n')
            fence = '`' * max(3, max([len(m[0]) + 1 for m in re.finditer(r'`+', code)] or [3]))
            return '\n\n' + fence + '\n' + code + '\n' + fence + '\n\n'
        if name == 'code' and node.parent.name != 'pre':
            return '`' + inner.replace('`', '\\`') + '`'
        if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            return '\n\n' + '#' * int(name[1]) + ' ' + inner.strip() + '\n\n'
        if name == 'table':
            if node.select('[colspan], [rowspan]'):
                return '\n\n' + str(node) + '\n\n'
            rows = [[text(cell).strip().replace('|', '\\|').replace('\n', '<br>') for cell in row.find_all(['td', 'th'], recursive=False)] for row in node.select('tr')]
            rows = [r for r in rows if r]
            if not rows:
                return ''
            width = max(map(len, rows)); rows = [r + [''] * (width - len(r)) for r in rows]
            return '\n\n' + '\n'.join(['| ' + ' | '.join(rows[0]) + ' |', '| ' + ' | '.join(['---'] * width) + ' |'] + ['| ' + ' | '.join(r) + ' |' for r in rows[1:]]) + '\n\n'
        if name == 'hr':
            return '\n\n---\n\n'
        if name in ('p', 'div', 'figure', 'figcaption'):
            return '\n\n' + inner.strip() + '\n\n' if inner.strip() else ''
        return inner
    return re.sub(r'\n{3,}', '\n\n', text(soup)).strip()


def blocks_to_markdown(blocks):
    return _blocks_to_markdown(expand_tab_sections(blocks))


def _blocks_to_markdown(blocks):
    parts = []
    for block in blocks:
        kind = block['type']; title = html_to_markdown(block.get('title', '')).replace('\n', ' ')
        body = html_to_markdown(block.get('html', ''))
        data = block.get('data', {})
        if kind == 'heading':
            parts.append('#' * min(6, max(1, int(data.get('level', 2)))) + ' ' + title)
        elif kind == 'separator':
            parts.append('---')
        elif kind == 'code':
            code = data.get('code', '')
            fence = '`' * max(3, max([len(m[0]) + 1 for m in re.finditer(r'`+', code)] or [3]))
            parts.append(fence + 'wow-macro\n' + code + '\n' + fence)
        elif kind in COMPONENTS:
            config = {k: data[k] for k in ('source_code', 'code', 'talent_version') if data.get(k)}
            if not config and data.get('decoded'):
                config['decoded'] = data['decoded']
            # 组件数据收在正文内的一段扩展围栏；不依赖侧栏记录或数据库块编号。
            content = '\n\n'.join(p for p in [body, _blocks_to_markdown(block.get('children', [])).strip()] if p)
            parts.append(':::' + kind + (' ' + title if title else '') + '\n```json\n' + json.dumps(config, ensure_ascii=False, indent=2) + '\n```\n' + (content + '\n' if content else '') + ':::')
        elif kind in EXTENSIONS:
            children = _blocks_to_markdown(block.get('children', []))
            parts.append(':::' + kind + (' ' + title if title else '') + '\n' + '\n\n'.join(p for p in [body, children] if p) + '\n:::')
        else:
            parts.append(body)
            if block.get('children'):
                parts.append(_blocks_to_markdown(block['children']))
    return '\n\n'.join(p for p in parts if p).strip() + '\n'


def normalize_tab_markdown(source):
    """只改选项卡围栏及相关标题；代码、链接和组件参数不重新序列化。"""
    if not isinstance(source, str):
        raise ValueError('Markdown 正文必须是文本')
    if not re.search(r'^:::tabs?(?:\s|$)', source, re.MULTILINE):
        return source
    blocks = compile_markdown(source, _expand_tabs=False)
    lines = source.splitlines(keepends=True)

    def replace(block, level):
        data = block.get('data', {})
        start = data['source_line']
        if block['type'] in {'tabs', 'tab'}:
            title = DIRECTIVE.fullmatch(lines[start].rstrip('\r\n'))[2] or ''
            if level and not title:
                title = '方案 {}'.format(data['tab_index'])
            lines[start] = '\n' + '#' * level + ' ' + title + '\n\n' if level else '\n'
            lines[data['source_end_line'] - 1] = '\n'
        else:
            lines[start] = '#' * level + ' ' + data['source_heading'] + '\n'
            for index in range(start + 1, data['source_end_line']):
                lines[index] = ''

    expand_tab_sections(blocks, replace)
    return ''.join(lines)


def compile_markdown(source, *, _expand_tabs=True):
    if not isinstance(source, str) or len(source.encode('utf-8')) > 4000000:
        raise ValueError('Markdown 正文必须是文本，且不超过 4 MB')
    md = MarkdownIt('commonmark', {'html': True}).enable('table')
    lines, sequence = source.splitlines(), 0
    def key():
        nonlocal sequence
        sequence += 1
        if sequence > 5000:
            raise ValueError('文章内容过长')
        return 'md-' + str(sequence)
    def ordinary(text, start_line=0):
        result, pending = [], []
        def flush():
            if pending:
                result.append({'id': key(), 'type': 'html', 'html': clean_html(md.renderer.render(pending, md.options, {})), 'children': []})
                pending.clear()
        tokens = md.parse(text); index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type == 'heading_open' and token.level == 0:
                flush()
                result.append({'id': key(), 'type': 'heading', 'title': clean_html(md.renderInline(tokens[index + 1].content)),
                    'data': {'level': int(token.tag[1]), 'source_line': start_line + token.map[0],
                             'source_end_line': start_line + token.map[1], 'source_heading': tokens[index + 1].content}, 'children': []})
                index += 3
                continue
            if token.type == 'fence' and token.level == 0 and token.info.strip() == 'wow-macro':
                flush(); result.append({'id': key(), 'type': 'code', 'data': {'code': token.content.rstrip('\n')}, 'children': []})
            else:
                pending.append(token)
            index += 1
        flush()
        return result
    def parse(index=0, depth=0, nested=False):
        if depth > 18:
            raise ValueError('扩展语法嵌套过深')
        result, pending = [], []
        fence = None
        def flush():
            if pending:
                result.extend(ordinary('\n'.join(pending), index - len(pending))); pending.clear()
        while index < len(lines):
            line = lines[index]
            marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
            if marker:
                if fence is None:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = None
                pending.append(line); index += 1; continue
            if fence:
                pending.append(line); index += 1; continue
            if line.strip() == ':::':
                if not nested:
                    raise ValueError('第 {} 行存在多余的扩展语法结束符'.format(index + 1))
                flush(); return result, index + 1
            match = DIRECTIVE.fullmatch(line)
            if match:
                flush()
                kind, title = match[1], match[2] or ''
                if kind not in EXTENSIONS:
                    raise ValueError('第 {} 行包含未知扩展：{}'.format(index + 1, kind))
                block = {'id': key(), 'type': kind, 'title': clean_html(md.renderInline(title)), 'data': {}}
                if kind in {'tabs', 'tab'}:
                    block['data'] = {'source_line': index, 'tab_index': 1 + sum(b['type'] == 'tab' for b in result)}
                index += 1
                if kind in COMPONENTS and index < len(lines) and lines[index].strip() == '```json':
                    config_lines = []; index += 1
                    while index < len(lines) and lines[index].strip() != '```':
                        config_lines.append(lines[index]); index += 1
                    if index == len(lines):
                        raise ValueError('组件 JSON 围栏未闭合')
                    config = json.loads('\n'.join(config_lines))
                    if not isinstance(config, dict):
                        raise ValueError('组件参数必须是 JSON 对象')
                    block['data'] = config; index += 1
                children, index = parse(index, depth + 1, True)
                block['children'] = children
                if kind in {'tabs', 'tab'}:
                    block['data']['source_end_line'] = index
                if kind in COMPONENTS:
                    data = block['data']; data['converted'] = False
                    if kind == 'talents':
                        data['code'] = str(data.get('code') or data.get('source_code') or '').replace('-', '+').replace('_', '/')
                        if not re.fullmatch(r'[A-Za-z0-9+/=]{8,4096}', data['code']):
                            raise ValueError('天赋扩展需要有效的 code 参数')
                        data['converted'] = True
                    elif data.get('source_code'):
                        data['decoded'] = decode_component('paperdoll' if kind == 'gear' else kind, str(data['source_code']))
                        data['converted'] = True
                    elif isinstance(data.get('decoded'), dict):
                        data['converted'] = True
                    if data.get('decoded') and not children:
                        block['html'] = clean_html(component_html('paperdoll' if kind == 'gear' else kind, data['decoded']))
                result.append(block)
                continue
            pending.append(line); index += 1
        if nested:
            raise ValueError('扩展语法缺少结束符 :::')
        flush(); return result, index
    blocks, _ = parse()
    return validate_blocks(expand_tab_sections(blocks) if _expand_tabs else blocks)
