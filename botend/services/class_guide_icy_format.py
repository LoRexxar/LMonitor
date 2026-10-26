"""Icy 语义组件及旧完整包的可校验格式恢复。"""
from functools import lru_cache
import hashlib
import html
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from botend.services.class_guide_content import walk_blocks


PERFORMANCE_LABELS = {'Damage': '伤害表现', 'Healing': '治疗表现', 'Raid': '团队副本',
                      'Mythic+': '大秘境', 'Utility': '团队功能', 'Survivability': '生存能力', 'Mobility': '机动性'}
RELATIONS = {0: ('', ''), 1: ('＞', '优先于'), 2: ('＝', '优先级相同'), 3: ('≥', '优先或相当于'), 4: ('≫', '明显优先于')}
STAT_NAMES = {'Item Level': '物品等级', 'Intellect': '智力', 'Agility': '敏捷', 'Strength': '力量',
              'Haste': '急速', 'Critical Strike': '暴击', 'Mastery': '精通', 'Versatility': '全能'}


def is_inline_node(node):
    if isinstance(node, Comment):
        return False
    if isinstance(node, NavigableString):
        return True
    return (isinstance(node, Tag) and node.name in {'a', 'strong', 'b', 'em', 'i', 'u', 's', 'mark', 'span', 'code', 'img', 'br'}
            and not any(c == 'centered' or c.startswith(('heading_number', 'export-string')) for c in node.get('class', [])))


def legacy_inline_groups(soup):
    """只提供原始 DOM 同一行内序列的证据；游戏引用复杂序列不靠文本猜拼。"""
    body = soup.select_one('.guide-page-content')
    if not body:
        return []
    groups = []
    for parent in [body, *body.find_all(['div', 'fieldset'])]:
        if parent.find_parent(['p', 'li', 'table', 'pre', 'code']):
            continue
        pending = []
        for node in [*parent.children, None]:
            if node is not None and is_inline_node(node):
                pending.append(node)
                continue
            if not any(isinstance(n, Tag) and (n.has_attr('data-iv-tooltip') or n.has_attr('data-wowhead') or n.select('[data-iv-tooltip],[data-wowhead]')) for n in pending):
                fragments = [n.get_text(' ', strip=True) if isinstance(n, Tag) else str(n).strip() for n in pending]
                fragments = [' '.join(t.split()) for t in fragments if t.strip()]
                if len(fragments) > 1 and len(''.join(fragments)) > 25:
                    groups.append(fragments)
            pending = []
    for paragraph in body.find_all('p'):
        if paragraph.select('[data-iv-tooltip],[data-wowhead]') or paragraph.find(['pre', 'code']):
            continue
        # 与历史导入的文本模式一致，兼容缓存文件里的 CRCRLF。
        markup = paragraph.decode_contents().replace('\r\n', '\n').replace('\r', '\n')
        fragments = re.split(r'\n\s*\n', markup)
        if len(fragments) > 1:
            fragments = [' '.join(BeautifulSoup(fragment, 'html.parser').get_text(' ', strip=True).split()) for fragment in fragments]
            if all(fragments):
                groups.append(fragments)
    return groups


def localize_stat_label(label):
    if label in STAT_NAMES:
        return STAT_NAMES[label]
    for name, chinese in STAT_NAMES.items():
        if label.startswith(name):
            suffix = label[len(name):].strip()
            percent = re.fullmatch(r'to (\d+(?:\.\d+)?%)', suffix)
            rating = re.fullmatch(r'\((?:to|until) (\d+\+?)(?: rating)?\)', suffix)
            if percent:
                return chinese + '（达到 ' + percent[1] + '）'
            if rating:
                return chinese + '（达到 ' + rating[1] + ' 等级）'
    raise ValueError('属性组件含未识别名称：' + label)


def stat_widget_data(node):
    """读取真实图标关系，不把等号或大于等于误当普通排序。"""
    names = [item.get_text(' ', strip=True) for item in node.select('.stat-name')]
    relations = []
    for separator in node.select('.stat-separator'):
        if separator.select_one('.greater-equal'):
            relations.append(3)
        elif separator.select_one('.ri-equal-line'):
            relations.append(2)
        elif separator.select_one('.ri-arrow-drop-right-line'):
            relations.append(1)
        else:
            raise ValueError('属性优先级含未知关系，不能推定顺序')
    if len(names) < 2 or len(relations) != len(names) - 1:
        raise ValueError('属性优先级名称与关系数量不匹配')
    return {'stat_labels': names, 'relation': relations}


def priority_html(labels, relations):
    items = []
    for index, label in enumerate(labels):
        relation = ''
        if index:
            symbol, description = RELATIONS.get(relations[index - 1], ('·', '随后列出'))
            relation = f'<span class="guide-stat-relation" aria-label="{description}">{symbol}</span>'
        items.append(f'<li>{relation}<strong>{html.escape(str(label))}</strong></li>')
    return '<ol class="guide-stat-priority" aria-label="属性优先级">' + ''.join(items) + '</ol>'


@lru_cache(maxsize=1)
def _legacy_repairs():
    path = Path(__file__).resolve().parents[1] / 'data/class_guides/icy-format-repairs-20261022.json'
    return json.loads(path.read_text(encoding='utf-8'))['articles']


def restore_icy_format(blocks, guide):
    """只命中经过原始 HTML 校验的正文版本；不改稿件或猜测人工编辑的结构。"""
    repairs = _legacy_repairs().get(getattr(guide, 'slug', ''), {})
    content = getattr(guide, 'content_markdown', '')
    if not repairs or hashlib.sha256(content.encode()).hexdigest() != repairs['content_sha256']:
        return
    for block in walk_blocks(blocks):
        operations = repairs['blocks'].get(block['id'], [])
        if not operations:
            continue
        soup = BeautifulSoup(block.get('html', ''), 'html.parser', preserve_whitespace_tags={'pre', 'code'})
        paragraphs = soup.find_all('p')
        for operation in operations:
            start = operation['paragraph']
            nodes = paragraphs[start:start + len(operation['text'])]
            if [node.get_text(' ', strip=True) for node in nodes] != operation['text']:
                raise ValueError('旧攻略格式修复的段落校验失败：' + block['id'])
            if operation['kind'] == 'paragraph':
                for node in nodes[1:]:
                    left, right = nodes[0].get_text(), node.get_text()
                    if left and right and re.fullmatch(r'[A-Za-z0-9]{2}', left[-1] + right[0]):
                        nodes[0].append(' ')
                    for child in list(node.contents):
                        nodes[0].append(child.extract())
                    node.decompose()
                continue
            if operation['kind'] == 'priority':
                markup = priority_html(operation['text'], operation['relation'])
            else:
                markup = ('<h4 class="guide-performance-heading">' + html.escape(operation['label'])
                          + '<span>' + html.escape(operation['rating']) + '</span></h4>')
                nodes[0]['class'] = ['guide-performance-summary']
                nodes[0].insert_before(BeautifulSoup(markup, 'html.parser'))
                continue
            nodes[0].insert_before(BeautifulSoup(markup, 'html.parser'))
            for node in nodes:
                node.decompose()
        block['html'] = str(soup)
