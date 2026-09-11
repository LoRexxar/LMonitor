"""按手册难度解析文本；动态缩放值保留语义，不伪造玩家等级数值。"""
import html
import re
from collections import defaultdict

from botend.services.simc_benchmark_tooltip_generator import _evaluate_constant_expression


def integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def index(rows):
    return {integer(row['ID']): row for row in rows}


def grouped(rows, field):
    result = defaultdict(list)
    for row in rows:
        result[integer(row.get(field))].append(row)
    return result


def number(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else f'{value:.2f}'.rstrip('0').rstrip('.')


def difficulty_text(text, difficulty):
    # 技能表还使用 $ ? DIFF 的双分支语法；方括号内可再含链接或条件。
    condition = re.compile(r'(?:\$\?|\?)((?:DIFF\d+)(?:\|DIFF\d+)*)', re.I)
    def group(position):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text) or text[position] != '[':
            return None
        start, depth = position + 1, 1
        for end in range(start, len(text)):
            depth += (text[end] == '[') - (text[end] == ']')
            if depth == 0:
                return text[start:end], end + 1
        return None

    def choice(position, depth=0):
        match = condition.match(text, position)
        if not match or depth > 20:
            return None
        yes = group(match.end())
        if not yes:
            return None
        no = choice(yes[1], depth + 1) if condition.match(text, yes[1]) else group(yes[1])
        if not no:
            return None
        ids = {int(i) for i in re.findall(r'\d+', match[1])}
        return yes[0] if difficulty in ids else no[0], no[1]

    pieces, position = [], 0
    while match := re.search(r'\$\?DIFF\d+', text[position:], flags=re.I):
        start = position + match.start()
        parsed = choice(start)
        if parsed is None:
            pieces.append(text[position:start + 2])
            position = start + 2
        else:
            pieces.extend((text[position:start], difficulty_text(parsed[0], difficulty)))
            position = parsed[1]
    text = ''.join(pieces) + text[position:]
    # $[!16 ... $] 中的 ! 是客户端手册难度语法标记，不是逻辑取反。
    # 仅识别 $[ / $]，正文中的 [技能链接] 不应结束条件块。
    marker = re.compile(r'\$\[(!?\d+(?:\s*,\s*!?\d+)*)|\$\]')
    enabled = [True]
    output = []
    position = 0
    for match in marker.finditer(text):
        if enabled[-1]:
            output.append(text[position:match.start()])
        if match[1] is not None:
            ids = {int(i) for i in re.findall(r'\d+', match[1])}
            enabled.append(enabled[-1] and difficulty in ids)
        elif len(enabled) > 1:
            enabled.pop()
        position = match.end()
    if enabled[-1]:
        output.append(text[position:])
    return ''.join(output)


class JournalText:
    def __init__(self, tables):
        self.spells = index(tables['Spell'])
        self.names = index(tables['SpellName'])
        self.effects = grouped(tables['SpellEffect'], 'SpellID')
        self.misc = grouped(tables['SpellMisc'], 'SpellID')
        self.auras = grouped(tables['SpellAuraOptions'], 'SpellID')
        self.targets = grouped(tables['SpellTargetRestrictions'], 'SpellID')
        self.duration = index(tables['SpellDuration'])
        self.radius = index(tables['SpellRadius'])
        self.ranges = index(tables['SpellRange'])
        self.difficulties = index(tables['Difficulty'])

    def select(self, rows, difficulty):
        visited = set()
        while difficulty not in visited:
            visited.add(difficulty)
            candidates = [r for r in rows if integer(r.get('DifficultyID')) == difficulty]
            if candidates:
                return candidates
            if not difficulty:
                break
            difficulty = integer(self.difficulties.get(difficulty, {}).get('FallbackDifficultyID'))
        return []

    def resolve(self, text, spell_id, difficulty, seen=()):
        dynamic = []
        text = difficulty_text(str(text or ''), difficulty)
        text = re.sub(r'\$bullet;', '• ', text, flags=re.I)
        text = re.sub(r'\$(?=\r?\n)', '', text)
        def embedded(match):
            target = int(match[2])
            if match[1].lower() == 'spellname':
                return self.names.get(target, {}).get('Name_lang') or f'技能 {target}'
            if match[1].lower() == 'spellicon':
                return ''
            if target in seen or len(seen) > 8:
                dynamic.append(match[0])
                return '（关联技能说明）'
            field = 'AuraDescription_lang' if match[1].lower() == 'spellaura' else 'Description_lang'
            source = self.spells.get(target, {}).get(field)
            if not source:
                dynamic.append(match[0])
                return self.names.get(target, {}).get('Name_lang') or f'技能 {target}'
            result, missing = self.resolve(source, target, difficulty, (*seen, target))
            dynamic.extend(missing)
            return result
        text = re.sub(r'\$@(spellname|spelldesc|spellaura|spelltooltip|spellicon)(\d+)', embedded, text, flags=re.I)

        def token(match, preserve_sign=False):
            sid = integer(match[1]) or spell_id
            kind = match[2].lower()
            effect_index = max(0, integer(match[3], 1) - 1)
            effects = [r for r in self.effects.get(sid, []) if integer(r.get('EffectIndex')) == effect_index]
            effect = next(iter(self.select(effects, difficulty)), {})
            misc = next(iter(self.select(self.misc.get(sid, []), difficulty)), {})
            value = None
            if kind in ('s', 'm') and effect:
                # 伤害系数及按生物等级缩放需要游戏运行时，不能显示为基础系数或零。
                scaling = any(float(effect.get(k) or 0) for k in ('Coefficient', 'EffectBonusCoefficient', 'BonusCoefficientFromAP',
                                                                'EffectRealPointsPerLevel', 'ResourceCoefficient'))
                scaling = scaling or integer(effect.get('Effect')) == 2 or (
                    integer(effect.get('Effect')) == 6 and integer(effect.get('EffectAura')) in (3, 301))
                scaling = scaling or float(effect.get('GroupSizeBasePointsCoefficient') or 1) != 1
                if not scaling:
                    value = float(effect.get('EffectBasePointsF', effect.get('EffectBasePoints', 0)) or 0)
            elif kind == 't' and effect:
                value = float(effect.get('EffectAuraPeriod') or 0) / 1000
            elif kind == 'd' and misc:
                duration = self.duration.get(integer(misc.get('DurationIndex')), {})
                if duration:
                    value = float(duration.get('Duration') or 0) / 1000
            elif kind == 'a' and effect:
                slot = 0 if match[2] == 'a' else 1
                radius_id = integer(effect.get(f'EffectRadiusIndex_{slot}')) or integer(effect.get(f'EffectRadiusIndex_{1-slot}'))
                radius = self.radius.get(radius_id, {})
                if radius:
                    value = float(radius.get('Radius') or 0)
            elif kind == 'r' and misc:
                row = self.ranges.get(integer(misc.get('RangeIndex')), {})
                if row:
                    value = float(row.get('RangeMax_0') or 0)
            elif kind in ('u', 'i'):
                row = next(iter(self.select((self.auras if kind == 'u' else self.targets).get(sid, []), difficulty)), {})
                field = 'CumulativeAura' if kind == 'u' else 'MaxTargets'
                if field in row:
                    value = float(row[field])
            if value is not None and (value >= 0 or kind in ('s', 'm')):
                if kind in ('s', 'm') and not preserve_sign:
                    value = abs(value)
                return number(value) + ('秒' if kind == 'd' else '')
            dynamic.append(match[0])
            return '（动态数值）'

        variables = re.compile(r'\$(\d+)?([sSmMaAtTdDuUiIrR])(\d*)')
        def expression(match):
            result = _evaluate_constant_expression(variables.sub(lambda m: token(m, preserve_sign=True), match[1]))
            if result is not None:
                return number(result)
            dynamic.append(match[0])
            return '（动态数值）'
        text = re.sub(r'\$\{([^{}]*)\}(?:\.\d+)?', expression, text)
        text = variables.sub(token, text)
        text = re.sub(r'\$[lL]([^:;]*):([^;]*);', lambda m: m[2], text)
        def unknown(match):
            dynamic.append(match[0])
            return '（随战斗条件变化）'
        text = re.sub(r'\$\?[^\[]*\[[^\]]*\](?:\[[^\]]*\])?|\$<[^>]*>|\$[^\s，。；、|<]*', unknown, text)
        text = re.sub(r'\|c[0-9a-fA-F]{8}|\|r', '', text)
        text = re.sub(r'\|H(?:spell|journal|item):[^|]*\|h(.*?)\|h', r'\1', text)
        text = re.sub(r'\|[TA].*?\|[ta]', '', text)
        text = re.sub(r'<[^>]*>', '', text)
        text = html.unescape(text).replace('秒秒', '秒').replace('\r', '')
        return text.strip(), sorted(set(dynamic))
