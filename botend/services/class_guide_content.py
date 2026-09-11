"""攻略内容块协议、安全引用语法及本地元数据解析。"""

import copy
import html
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString

from botend.services.wow_localization import names_for, version_for
from botend.templatetags.wow_tags import wow_icon_oss_url


REF_RE = re.compile(r'\[\[(spell|item|talent):(\d+)(?:@([A-Za-z0-9_-]+))?\]\]')
TYPES = {'html', 'heading', 'tabs', 'tab', 'columns', 'column', 'accordion', 'details',
         'callout', 'rating', 'changelog', 'code', 'image', 'talents', 'gear',
         'rotation', 'priority', 'timeline', 'simulation', 'group', 'separator', 'unsupported'}
TAGS = {'p', 'strong', 'b', 'em', 'i', 'u', 's', 'mark', 'span', 'br', 'ul', 'ol', 'li',
        'table', 'thead', 'tbody', 'tr', 'th', 'td', 'blockquote', 'a', 'figure',
        'figcaption', 'img', 'code', 'pre', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'div'}


def safe_url(value, image=False):
    value = str(value or '').strip()
    parsed = urlparse(value)
    if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
        return value
    if not image and value.startswith(('/', '#')) and not value.startswith('//') and '\\' not in value:
        return value
    return ''


def clean_html(value, references=None):
    soup = BeautifulSoup(str(value or ''), 'html.parser')
    for tag in list(soup.find_all(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'form', 'button', 'input'])):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        classes = tag.get('class', [])
        kind = next((k for c, k in [('wow-spell', 'spell'), ('wow-item', 'item'), ('wow-trait', 'talent')] if c in classes), None)
        if kind:
            raw = tag.get('data-wow-item') or tag.get('data-wow-id') or ''
            match = re.fullmatch(r'(\d+)(?::([A-Za-z0-9_-]+))?', raw)
            if match:
                broken = re.fullmatch(r'(.*?)\[/wow-(spell|trait|item)\](.*?)\[wow-(spell|trait|item)\s+id=(\d+)[^\]]*\](.*)', tag.get_text(), re.S)
                if broken:
                    # 来源偶发将两个 BBCode 引用错误合为一个 span，按其明确的结束/开始标记恢复。
                    kinds = {'spell': 'spell', 'trait': 'talent', 'item': 'item'}
                    first_kind, second_kind = kinds[broken[2]], kinds[broken[4]]
                    first = '[[{}:{}{}]]'.format(first_kind, match[1], '@' + match[2] if match[2] else '')
                    second = '[[{}:{}]]'.format(second_kind, broken[5])
                    if references is not None:
                        for token, ref_kind, object_id, name in [(first, first_kind, match[1], broken[1]), (second, second_kind, broken[5], broken[6])]:
                            references[token] = {'kind': ref_kind, 'id': int(object_id), 'source_name': name.strip(),
                                'repair_note': '恢复来源合并错误的 BBCode 引用'}
                    tag.replace_with(NavigableString(first + broken[3] + second))
                    continue
                token = '[[{}:{}{}]]'.format(kind, match[1], '@' + match[2] if match[2] else '')
                if references is not None:
                    references[token] = {'kind': kind, 'id': int(match[1]), 'source_name': tag.get_text(' ', strip=True), 'variant': match[2] or ''}
                tag.replace_with(NavigableString(token))
                continue
        if tag.name not in TAGS:
            tag.unwrap()
            continue
        attrs = {}
        for key in ('href', 'src'):
            if key in tag.attrs:
                value = safe_url(tag.get(key), image=key == 'src')
                if value:
                    attrs[key] = value
        for key in ('alt', 'title'):
            if key in tag.attrs:
                attrs[key] = str(tag[key])[:1000]
        for key in ('colspan', 'rowspan', 'start'):
            if str(tag.get(key, '')).isdigit():
                attrs[key] = str(min(int(tag[key]), 100))
        if tag.name == 'a':
            attrs['rel'] = 'noopener noreferrer'
        if tag.name == 'img':
            attrs.update(loading='lazy', referrerpolicy='no-referrer')
        tag.attrs = attrs
    # 来源编辑器偶尔把一个英文单词拆在多层强调标签之间。
    # 只去掉切开单词的样式边界，保留可见文字和完整词组外层的样式。
    strings = list(soup.find_all(string=True))
    offsets, position = {}, 0
    for text in strings:
        offsets[id(text)] = (position, position + len(text))
        position += len(text)
    visible = ''.join(str(text) for text in strings)
    for tag in list(soup.find_all(['strong', 'b', 'em', 'i', 'u', 's', 'mark', 'span'])):
        if tag.find_parent(['code', 'pre']):
            continue
        children = list(tag.find_all(string=True))
        if not children:
            continue
        start, end = offsets[id(children[0])][0], offsets[id(children[-1])][1]
        if any(0 < boundary < len(visible) and re.fullmatch(r'[A-Za-z]{2}', visible[boundary-1:boundary+1]) for boundary in (start, end)):
            tag.unwrap()
    return str(soup).strip()


def walk_blocks(blocks):
    for block in blocks:
        yield block
        yield from walk_blocks(block.get('children', []))


def validate_blocks(blocks):
    if not isinstance(blocks, list) or len(str(blocks)) > 4000000:
        raise ValueError('内容必须是数组，且不超过 4 MB')
    ids = set()
    count = 0
    def visit(rows, depth=0):
        nonlocal count
        if depth > 18 or not isinstance(rows, list):
            raise ValueError('内容嵌套层级或结构无效')
        result = []
        for raw in rows:
            count += 1
            if count > 5000 or not isinstance(raw, dict) or raw.get('type') not in TYPES:
                raise ValueError('内容块类型无效或超过 5000 块')
            block = copy.deepcopy(raw)
            key = block.get('id')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key) or key in ids:
                raise ValueError('每个内容块需要唯一的 id')
            ids.add(key)
            for field in ('html', 'title'):
                if field in block:
                    if not isinstance(block[field], str):
                        raise ValueError('标题和正文必须是字符串')
                    block[field] = clean_html(block[field])
            if 'data' in block and not isinstance(block['data'], dict):
                raise ValueError('组件数据必须是对象')
            block['children'] = visit(block.get('children', []), depth + 1)
            result.append(block)
        return result
    return visit(blocks)


def _tooltip_text(*values):
    """合并站内事实字段，保留原文换行，去重并忽略明确的占位内容。"""
    lines = []
    seen = set()
    placeholders = {'placeholder', '[placeholder]'}
    for value in values:
        for line in str(value or '').replace('\r\n', '\n').split('\n'):
            line = line.strip()
            key = line.casefold()
            if line and key not in placeholders and key not in seen:
                seen.add(key)
                lines.append(line)
    return '\n'.join(lines)


def _reference_fallback_tooltip(ref, row=None):
    """为没有效果正文的显式引用提供诚实、可交互的站内说明。"""
    identity = int(ref['id'])
    kind = ref['kind']
    if kind == 'talent' and getattr(row, 'tree_type', '') == 'hero_anchor':
        return '英雄天赋专精\n该条目用于标识英雄天赋专精分支，不是可施放技能。\n引用 ID：{}'.format(identity)
    if kind == 'talent':
        return '天赋\n当前版本暂无可用的效果正文。\n引用 ID：{}'.format(identity)
    if kind == 'spell':
        return '技能\n当前版本暂无可用的效果正文。\n技能 ID：{}'.format(identity)
    return '物品\n当前版本暂无可用的物品说明。\n物品 ID：{}'.format(identity)


def _spell_snapshot_metadata(game_version, spell_ids):
    """按当前权威分支和精确 Spell ID 批量读取站内当前技能快照。"""
    spell_ids = {int(value) for value in spell_ids or () if value}
    if not spell_ids:
        return {}
    from botend.services.wow_localization import current_reference_version
    version = current_reference_version()
    if not version:
        return {}
    from botend.models import WowSpellSnapshot

    branch_order = ['wowxptr', 'wowt'] if version.branch == 'ptr' else ['wow']

    def branch_rank(branch):
        try:
            return len(branch_order) - branch_order.index(branch)
        except ValueError:
            return 0

    def build_rank(build):
        return tuple(int(value) for value in re.findall(r'\d+', str(build or '')))

    rows = WowSpellSnapshot.objects.filter(spell_id__in=spell_ids)
    candidates = {}
    for row in rows:
        identity = int(row.spell_id)
        entry = candidates.setdefault(identity, {
            'tooltip': '', 'tooltip_rank': None, 'icon': '', 'icon_rank': None,
        })
        body = _tooltip_text(row.description, row.aura_description)
        body_rank = (
            branch_rank(row.branch),
            bool(re.search(r'[\u3400-\u9fff]', body)),
            row.locale == 'zhCN',
            bool(body),
            build_rank(row.snapshot_build),
        )
        if entry['tooltip_rank'] is None or body_rank > entry['tooltip_rank']:
            entry['tooltip'] = body
            entry['tooltip_rank'] = body_rank
        icon = str(row.icon or '').strip()
        icon_rank = (
            branch_rank(row.branch), bool(icon), row.locale == 'zhCN', build_rank(row.snapshot_build),
        )
        if entry['icon_rank'] is None or icon_rank > entry['icon_rank']:
            entry['icon'] = icon
            entry['icon_rank'] = icon_rank
    return {
        spell_id: {'tooltip': entry['tooltip'], 'icon': entry['icon']}
        for spell_id, entry in candidates.items()
    }


def resolve_references(blocks, game_version, class_name='', spec_name='', source_refs=None):
    """批量解析引用；不会把缺失的官方中文自动猜译成已校验词条。"""
    content = BeautifulSoup('\n'.join(b.get('title', '') + b.get('html', '') for b in walk_blocks(blocks)), 'html.parser')
    for code in content.select('pre, code'):
        code.decompose()
    refs = {m[0]: {'kind': m[1], 'id': int(m[2]), 'variant': m[3] or ''}
            for m in REF_RE.finditer(content.get_text())}
    if not refs:
        return {}
    # 显式引用由 (kind, ID) 唯一标识，不受攻略职业/专精上下文影响。
    records = names_for(game_version, reference_ids={kind: {r['id'] for r in refs.values() if r['kind'] == kind} for kind in ('spell', 'item', 'talent')})
    source_refs = source_refs or {}
    selected_rows = {}
    spell_ids = set()
    item_ids = set()
    for token, ref in refs.items():
        source_name = (source_refs.get(token) or {}).get('source_name', '')
        if ref['kind'] == 'talent':
            talent_records = [r for r in records if r['kind'] == 'talent']
            # 攻略 talent:ID 是来源 TraitNodeEntry.ID。显式核验映射优先，
            # 其次按当前版本 Entry ID 匹配；alias 仅兼容已核验的历史来源编号。
            candidate_tiers = (
                [r for r in talent_records if r.get('reference_id') == ref['id']],
                [r for r in talent_records if r.get('node_id') == ref['id']],
                [r for r in talent_records if ref['id'] in r['aliases']],
            )
            candidates = next((tier for tier in candidate_tiers if tier), [])
        else:
            candidates = [r for r in records if r['kind'] == ref['kind'] and r['object_id'] == ref['id']]
        if not candidates and ref['kind'] == 'talent' and source_name:
            candidates = [r for r in records if r['kind'] == 'talent' and r['name_en'].casefold() == source_name.casefold()]
            if len({r['name_zh'] for r in candidates}) > 1:
                candidates = []
        if not candidates and ref['kind'] == 'spell':
            candidates = [r for r in records if r['kind'] == 'talent' and ref['id'] in (r.get('spell_id'), r.get('display_spell_id'))]
        # 相同 ID 在历史版本改名时，只在来源名精确匹配的候选中选择。
        matched = [r for r in candidates if source_name and reference_names_match(source_name, r['name_en'])]
        selected = next(iter(matched or candidates), None)
        from types import SimpleNamespace
        row = SimpleNamespace(**selected) if selected else None
        selected_rows[token] = row
        evidence = row.evidence if row else ''
        name = (getattr(row, 'name_zh', '') or (getattr(row, 'name', '') if getattr(row, 'locale', '') == 'zhCN' else '')) if row else ''
        if not re.search(r'[\u3400-\u9fff]', name):
            name = ''
        icon = getattr(row, 'icon', '') if row else ''
        icon = wow_icon_oss_url(icon, size='small') if icon else ''
        ref.update(name=name or '待校订的{} {}'.format({'spell': '技能', 'item': '物品', 'talent': '天赋'}[ref['kind']], ref['id']),
                   resolved=bool(name), icon=safe_url(icon, image=True), evidence=evidence or ('站内元数据' if name else ''),
                   name_en=(getattr(row, 'name_en', '') or getattr(row, 'name', '')) if row else '',
                   source_name=(source_refs.get(token) or {}).get('source_name', ''),
                   tooltip_title=name or '')
        if ref['kind'] == 'spell':
            spell_ids.add(ref['id'])
        elif ref['kind'] == 'item':
            item_ids.add(ref['id'])
        elif row and not _tooltip_text(getattr(row, 'description_zh', '') or getattr(row, 'description', '')):
            linked_spell_id = getattr(row, 'display_spell_id', None)
            if linked_spell_id:
                spell_ids.add(linked_spell_id)

    spell_metadata = _spell_snapshot_metadata(game_version, spell_ids)
    if item_ids:
        from botend.services.wow_item_display import load_item_display_metadata
        item_tooltips = load_item_display_metadata(item_ids)
    else:
        item_tooltips = {}
    for token, ref in refs.items():
        row = selected_rows[token]
        tooltip = ''
        source = ''
        fallback_icon = ''
        if ref['kind'] == 'talent' and row:
            tooltip = _tooltip_text(getattr(row, 'description_zh', '') or getattr(row, 'description', ''))
            source = 'talent_metadata' if tooltip else ''
            linked_spell_id = getattr(row, 'display_spell_id', None)
            linked_metadata = spell_metadata.get(linked_spell_id, {})
            fallback_icon = linked_metadata.get('icon', '')
            if not tooltip:
                tooltip = linked_metadata.get('tooltip', '')
                source = 'spell_snapshot' if tooltip else ''
        elif ref['kind'] == 'spell':
            metadata = spell_metadata.get(ref['id'], {})
            tooltip = metadata.get('tooltip', '')
            fallback_icon = metadata.get('icon', '')
            source = 'spell_snapshot' if tooltip else ''
        elif ref['kind'] == 'item':
            metadata = item_tooltips.get(ref['id']) or {}
            tooltip = metadata.get('tooltip', '')
            fallback_icon = metadata.get('icon_url', '')
            source = 'item_snapshot' if tooltip else ''
        if not ref.get('icon') and fallback_icon:
            ref['icon'] = safe_url(wow_icon_oss_url(fallback_icon, size='small'), image=True)
        if not tooltip:
            tooltip = _reference_fallback_tooltip(ref, row)
            source = 'local_reference_fallback'
        ref.update(tooltip_text=tooltip, tooltip_source=source)
    return refs


def reference_names_match(source, official):
    """仅忽略标点、所有格、简单复数及明确的技能前缀，保留实质名称冲突。"""
    def normalize(value):
        value = str(value).casefold().replace('’', "'")
        value = re.sub(r"'s\b|s'\b", 's', value)
        value = re.sub(r'^(?:summon |mastery:\s*|shapeshift:\s*)', '', value)
        words = re.findall(r'[a-z0-9]+', value)
        return ' '.join(word[:-3] + 'y' if word.endswith('ies') else word[:-1] if word.endswith('s') and not word.endswith('ss') else word for word in words)
    return normalize(source) == normalize(official)


def talent_version_for(game_version):
    return version_for(game_version)


def render_references(value, references):
    soup = BeautifulSoup(clean_html(value), 'html.parser')
    for node in list(soup.find_all(string=True)):
        if node.find_parent(['code', 'pre']) or not REF_RE.search(str(node)):
            continue
        def replace(match):
            ref = references.get(match[0], {})
            name = html.escape(ref.get('name', '待校订引用 ' + match[2]))
            icon = ref.get('icon', '')
            icon = wow_icon_oss_url(icon, size='small') if icon else ''
            img = '<img src="{}" alt="" loading="lazy">'.format(html.escape(icon, quote=True)) if icon else ''
            classes = 'guide-ref{}'.format(' is-unresolved' if not ref.get('resolved') else '')
            attrs = 'class="{}" data-reference-kind="{}" data-reference-id="{}" aria-label="{}"'.format(
                classes,
                html.escape(str(ref.get('kind', '')), quote=True),
                html.escape(str(ref.get('id', '')), quote=True),
                html.escape(ref.get('source_name') or ref.get('name', ''), quote=True),
            )
            tooltip = ref.get('tooltip_text', '')
            if tooltip:
                attrs += ' data-guide-tooltip="" data-tooltip-title="{}" data-tooltip-body="{}" tabindex="0" role="button"'.format(
                    html.escape(ref.get('tooltip_title') or ref.get('name', ''), quote=True),
                    html.escape(tooltip, quote=True),
                )
            return '<span {}>{}{}</span>'.format(attrs, img, name)
        fragment = BeautifulSoup(REF_RE.sub(replace, html.escape(str(node))), 'html.parser')
        node.replace_with(fragment)
    return str(soup)
