"""只替换宏命令中的完整技能名称，保持条件、指令和 Lua 原样。"""
import re
from collections import defaultdict


def macro_name_map(rows):
    """同一英文存在多个中文时，不用数据库遍历顺序决定宏名称。"""
    candidates, phrases, overrides = defaultdict(set), {}, {}
    for kind, english, chinese in rows:
        if not english or not chinese:
            continue
        key = english.casefold()
        if kind == 'macro':
            overrides[key] = chinese
        elif kind == 'phrase':
            phrases[key] = chinese
        else:
            candidates[key].add(chinese)
    names = {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}
    names.update(phrases)
    names.update(overrides)
    return names


def macro_source_repairs(code):
    notes = []
    if re.search(r'\bstance\[:\d+\]', code):
        notes.append('原文姿态条件 stance[:数字] 的括号位置错误，已改为 [stance:数字]。')
    if re.search(r'(?mi)^\s*(?:/show\s+tooltip|showtooltip|#showtooltips|#showtooltip[A-Za-z])', code):
        notes.append('修正原文提示指令的缺失井号、空格或多余字母，统一为 #showtooltip。')
    return notes


def macro_block_references(blocks):
    """Scope source tokens to structural sections and preceding explanations.

    Introductory references remain local to that section. Later explanations
    apply to the next code block only. Headings and sibling containers reset
    scope; descendant or later references never leak backwards into a macro.
    """
    from botend.services.class_guide_content import REF_RE

    def refs(block):
        return [{'kind': kind, 'object_id': int(sid), 'source_block': block['id']}
                for kind, sid, _ in REF_RE.findall(block.get('title', '') + '\n' + block.get('html', ''))]

    result = {}

    def visit(rows, initial=()):
        intro, adjacent, seen_code = list(initial), [], False
        for block in rows:
            own = refs(block)
            if block['type'] in ('heading', 'separator') or re.search(r'<h[2-6]\b', block.get('html', ''), re.I):
                intro, adjacent, seen_code = own, [], False
            elif block['type'] == 'code':
                result[block['id']] = own or intro + adjacent
                adjacent, seen_code = [], True
            elif block.get('children'):
                # A new structural subsection owns its own introduction.
                visit(block['children'], own)
                intro, adjacent, seen_code = [], [], False
            elif seen_code:
                adjacent.extend(own)
            else:
                intro.extend(own)
    visit(blocks)
    return result


def macro_operands(code):
    """Use the rendering parser itself, retaining operand kind and all names."""
    operands = set()
    def collect(value, kind):
        operands.add((kind, value))
    localize_macro(code, {}, resolve=collect)
    return sorted(operands)


def resolve_macro_operands(operands, context, source_refs, unscoped_rows=()):
    """A unique Chinese label is not a unique entity; never use global overrides."""
    decisions = {}
    for kind, operand in operands:
        key = operand.casefold()
        candidates = {sid: row for sid, row in context['rows'].items()
                      if kind in ('spell', 'spell_or_item') and row['name'].casefold() == key}
        decision = {'operand': operand, 'kind': kind, 'status': 'missing_identity',
                    'candidate_ids': sorted(candidates), 'source_refs': source_refs,
                    'unscoped_candidates': sorted({(r['kind'], r['object_id']) for r in unscoped_rows
                        if r['kind'] in ('spell', 'talent', 'item') and r['name_en'].casefold() == key}),
                    'issues': context['issues']}
        # Named items/talent-entry anchors need their own proven namespace
        # projection. Do not mistake a same-name spell for an item in /use.
        if kind == 'item' or (kind == 'spell_or_item' and any(r['kind'] == 'item' for r in source_refs)):
            decision['status'] = 'incomplete_context'
            decision['issues'] = context['issues'] + [{'reason': 'unsupported_item_identity'}]
        elif context['issues']:
            decision['status'] = 'incomplete_context'
        elif len(candidates) > 1:
            decision['status'] = 'ambiguous_identity'
        elif candidates:
            sid, row = next(iter(candidates.items()))
            decision.update(identity=('spell', sid), branch=row['branch'],
                            snapshot_build=row['snapshot_build'],
                            relations=[edge for edge in context['edges'] if edge['to'] == sid])
            if not re.search(r'[\u3400-\u9fff]', row['name_zh']):
                decision['status'] = 'missing_zh'
            else:
                decision.update(status='resolved', name_zh=row['name_zh'])
        decisions[(kind, key)] = decision
    return decisions


def localize_macro(code, names, *, resolve=None):
    code = re.sub(r'\bstance\[:(\d+)\]', r'[stance:\1]', code)
    code = re.sub(r'(?mi)^(\s*)(?:/show\s+tooltip|showtooltip|#showtooltips)\b', r'\1#showtooltip', code)
    code = re.sub(r'(?mi)^(\s*#showtooltip)(?=[A-Za-z])', r'\1 ', code)
    missing = set()
    def name(value, kind='spell'):
        prefix = re.match(r'^\s*', value)[0]
        suffix = re.search(r'\s*$', value)[0]
        core = value.strip()
        if not core or re.fullmatch(r'(?:item:)?\d+|null', core, re.I) or not re.search(r'[A-Za-z]', core):
            return value
        toggle = '!' if core.startswith('!') else ''
        translated = (resolve(core.removeprefix('!'), kind) if resolve is not None
                      else names.get(core.removeprefix('!').casefold()))
        if translated:
            return prefix + toggle + translated + suffix
        missing.add(core.removeprefix('!'))
        return value
    def conditions(match):
        text = match[0]
        return re.sub(r'\b(known|noknown):([^,\]]+)', lambda m:m[1] + ':' + '/'.join(name(v) for v in m[2].split('/')), text)
    result = []
    for raw in code.splitlines(keepends=True):
        line = raw.rstrip('\r\n')
        ending = raw[len(line):]
        whisper = re.match(r'^(\s*/(?:w|whisper)\s+\S+\s+)(.+)$', line, re.I)
        if whisper:
            result.append((line if resolve is not None else whisper[1] + name(whisper[2])) + ending); continue
        match = re.match(r'^(\s*(?:/(cast|use|castsequence|castrandom|equip|cancelaura|petautocaston|petautocastoff|petautocasttoggle|target|tar|targetexact)|#show(?:tooltip|icon)?))(\s+)(.*)$', line, re.I)
        if not match:
            result.append(line + ending); continue
        command = (match[2] or '').lower()
        if resolve is not None and command in ('target', 'tar', 'targetexact'):
            result.append(line + ending); continue
        kind = 'item' if command == 'equip' else ('spell_or_item' if command == 'use' or not command else 'spell')
        args = re.sub(r'\[[^\]]*\]', conditions, match[4])
        pieces = re.split(r'(\[[^\]]*\]|\breset=[^\s]+\s*|;)', args)
        for index, piece in enumerate(pieces):
            if not piece or piece.startswith('[') or piece.startswith('reset=') or piece == ';':
                continue
            if (match[2] or '').lower() in ('target', 'tar', 'targetexact') and piece.strip().lower() in ('pet', 'player', 'target', 'focus', 'mouseover'):
                continue
            if (match[2] or '').lower() in ('castsequence', 'castrandom'):
                pieces[index] = ','.join(name(part) for part in piece.split(','))
            else:
                pieces[index] = name(piece, kind)
        result.append(match[1] + match[3] + ''.join(pieces) + ending)
    return ''.join(result), sorted(missing)
