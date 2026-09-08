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


def localize_macro(code, names):
    code = re.sub(r'\bstance\[:(\d+)\]', r'[stance:\1]', code)
    code = re.sub(r'(?mi)^(\s*)(?:/show\s+tooltip|showtooltip|#showtooltips)\b', r'\1#showtooltip', code)
    code = re.sub(r'(?mi)^(\s*#showtooltip)(?=[A-Za-z])', r'\1 ', code)
    missing = set()
    def name(value):
        prefix = re.match(r'^\s*', value)[0]
        suffix = re.search(r'\s*$', value)[0]
        core = value.strip()
        if not core or re.fullmatch(r'(?:item:)?\d+|null', core, re.I) or not re.search(r'[A-Za-z]', core):
            return value
        toggle = '!' if core.startswith('!') else ''
        translated = names.get(core.removeprefix('!').casefold())
        if translated:
            return prefix + toggle + translated + suffix
        missing.add(core.removeprefix('!'))
        return value
    def conditions(match):
        text = match[0]
        return re.sub(r'\b(known|noknown):([^,\]]+)', lambda m:m[1] + ':' + '/'.join(name(v) for v in m[2].split('/')), text)
    result = []
    for line in code.splitlines():
        whisper = re.match(r'^(\s*/(?:w|whisper)\s+\S+\s+)(.+)$', line, re.I)
        if whisper:
            result.append(whisper[1] + name(whisper[2])); continue
        match = re.match(r'^(\s*(?:/(cast|use|castsequence|castrandom|equip|cancelaura|petautocaston|petautocastoff|petautocasttoggle|target|tar|targetexact)|#show(?:tooltip|icon)?))(\s+)(.*)$', line, re.I)
        if not match:
            result.append(line); continue
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
                pieces[index] = name(piece)
        result.append(match[1] + match[3] + ''.join(pieces))
    return '\n'.join(result), sorted(missing)
