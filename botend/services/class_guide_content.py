"""攻略内容块协议、安全引用语法及本地元数据解析。"""

import copy
import html
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString
from django.db.models import Q

from botend.guide_models import ClassGuideTerm
from botend.constants.wow import canonical_class_spec
from botend.models import WowItemSnapshot, WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion


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


def resolve_references(blocks, game_version, class_name='', spec_name='', source_refs=None):
    """批量解析引用；不会把缺失的官方中文自动猜译成已校验词条。"""
    content = BeautifulSoup('\n'.join(b.get('title', '') + b.get('html', '') for b in walk_blocks(blocks)), 'html.parser')
    for code in content.select('pre, code'):
        code.decompose()
    refs = {m[0]: {'kind': m[1], 'id': int(m[2]), 'variant': m[3] or ''}
            for m in REF_RE.finditer(content.get_text())}
    if not refs:
        return {}
    overrides = {(r.kind, r.object_id): r for r in ClassGuideTerm.objects.filter(game_version=game_version)}
    version = talent_version_for(game_version)
    class_name, spec_name = canonical_class_spec(class_name, spec_name) or (class_name, spec_name)
    talents = WowTalentNodeMetadata.objects.none()
    if version:
        talents = WowTalentNodeMetadata.objects.filter(talent_version=version, class_name=class_name, spec_name=spec_name)
    talent_rows = list(talents.exclude(name_zh=''))
    spell_ids = {r['id'] for r in refs.values() if r['kind'] == 'spell'}
    branch = 'wowxptr' if version and version.branch == 'ptr' else 'wow'
    spells = {}
    for row in WowSpellSnapshot.objects.filter(spell_id__in=spell_ids, branch=branch).order_by('locale'):
        if row.name_zh or row.locale == 'zhCN':
            spells[row.spell_id] = row
    items = {r.item_id: r for r in WowItemSnapshot.objects.filter(item_id__in=[v['id'] for v in refs.values() if v['kind'] == 'item'])}
    source_refs = source_refs or {}
    for token, ref in refs.items():
        row = overrides.get((ref['kind'], ref['id']))
        evidence = row.evidence if row else ''
        if not row and ref['kind'] == 'talent':
            candidates = [r for r in talent_rows if ref['id'] in (r.talent_id, r.node_id)]
            if not candidates:
                source_name = (source_refs.get(token) or {}).get('source_name', '').casefold()
                candidates = [r for r in talent_rows if source_name and r.name.casefold() == source_name]
            if candidates and len({r.name_zh for r in candidates}) == 1:
                row = candidates[0]
        elif not row and ref['kind'] == 'spell':
            row = spells.get(ref['id'])
            if not row:
                row = next((r for r in talent_rows if ref['id'] in (r.spell_id, r.display_spell_id)), None)
        elif not row and ref['kind'] == 'item':
            row = items.get(ref['id'])
        name = (getattr(row, 'name_zh', '') or (getattr(row, 'name', '') if getattr(row, 'locale', '') == 'zhCN' else '')) if row else ''
        if not re.search(r'[\u3400-\u9fff]', name):
            name = ''
        icon = getattr(row, 'icon', '') if row else ''
        if icon and re.fullmatch(r'[A-Za-z0-9_-]+', icon):
            icon = 'https://wow.zamimg.com/images/wow/icons/large/' + icon.lower() + '.jpg'
        ref.update(name=name or '待校订的{} {}'.format({'spell': '技能', 'item': '物品', 'talent': '天赋'}[ref['kind']], ref['id']),
                   resolved=bool(name), icon=safe_url(icon, image=True), evidence=evidence or ('站内元数据' if name else ''),
                   name_en=(getattr(row, 'name_en', '') or getattr(row, 'name', '')) if row else '',
                   source_name=(source_refs.get(token) or {}).get('source_name', ''))
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
    version = WowTalentVersion.objects.filter(key=game_version).first()
    if version:
        return version
    return WowTalentVersion.objects.filter(Q(major_version=game_version) | Q(major_version=game_version + '.0') |
        Q(key__endswith='-' + game_version) | Q(key__endswith='-' + game_version + '.0')).order_by('-is_active', '-id').first()


def render_references(value, references):
    soup = BeautifulSoup(clean_html(value), 'html.parser')
    for node in list(soup.find_all(string=True)):
        if node.find_parent(['code', 'pre']) or not REF_RE.search(str(node)):
            continue
        def replace(match):
            ref = references.get(match[0], {})
            name = html.escape(ref.get('name', '待校订引用 ' + match[2]))
            icon = ref.get('icon', '')
            img = '<img src="{}" alt="" loading="lazy">'.format(html.escape(icon, quote=True)) if icon else ''
            return '<span class="guide-ref{}" title="{}">{}{}</span>'.format(' is-unresolved' if not ref.get('resolved') else '', html.escape(ref.get('source_name', ''), quote=True), img, name)
        fragment = BeautifulSoup(REF_RE.sub(replace, html.escape(str(node))), 'html.parser')
        node.replace_with(fragment)
    return str(soup)
