import ast
import math
import re


_EFFECT_HEADER_RE = re.compile(r'^#(?P<index>\d+)\s+\(id=(?P<id>\d+)\)')
_EFFECT_VALUES_RE = re.compile(
    r'Base Value:\s*(?P<base>-?\d+(?:\.\d+)?)\s*\|\s*'
    r'Scaled Value:\s*(?P<scaled>-?\d+(?:\.\d+)?)'
)
_DURATION_RE = re.compile(r'^Duration\s*:\s*(?P<seconds>\d+(?:\.\d+)?)\s+seconds?\s*$')
_STACKS_RE = re.compile(r'^Stacks\s*:\s*(?P<count>\d+)\s+maximum\s*$')
_PERIOD_RE = re.compile(r'\bevery\s+(?P<seconds>\d+(?:\.\d+)?)\s+seconds?\b', re.IGNORECASE)
_VERIFIED_TOKEN_RE = re.compile(
    r'\$(?P<spell_id>\d+)?(?P<kind>[dwut])(?P<effect_index>\d+)?(?![A-Za-z])'
    r'|\$(?P<value_spell_id>\d+)?(?P<value_kind>[sw])(?P<value_effect_index>\d+)',
    re.IGNORECASE,
)
_UNRESOLVED_TOKEN_RE = re.compile(
    r'\$\?[^\[]*\[[^\]]*\](?:\[[^\]]*\])?'
    r'|\$\{[^{}]+\}(?:\.\d+)?'
    r'|\$@(?:spellicon|spellname|spelldesc|spellaura)\d+'
    r'|\$\d*[a-zA-Z]\d*'
    r'|\$<[^>]+>'
)
_CONSTANT_EXPRESSION_RE = re.compile(r'\$\{(?P<expression>[^{}]+)\}')

_STAT_LABELS = {
    'stragiint': '力量/敏捷/智力', 'stragi': '力量/敏捷',
    'strint': '力量/智力', 'agiint': '敏捷/智力',
    'strength': '力量', 'agility': '敏捷', 'intellect': '智力',
    'crit': '暴击', 'haste': '急速', 'mastery': '精通',
    'versatility': '全能', 'stamina': '耐力', 'armor': '护甲',
}
_STAT_ORDER = {key: index for index, key in enumerate(_STAT_LABELS)}


def _number(value):
    number = float(value)
    return int(number) if number.is_integer() else number


def render_item_stats(stats):
    """Render SimC's already-scaled gear stats in stable tooltip order."""
    rows = []
    for key, value in sorted(
        (stats or {}).items(), key=lambda pair: (_STAT_ORDER.get(pair[0], 999), pair[0]),
    ):
        label = _STAT_LABELS.get(key)
        if not label or not isinstance(value, (int, float)) or isinstance(value, bool) or value == 0:
            continue
        rows.append(f'{value:+,} {label}')
    return rows


def normalize_tooltip_text(value):
    """Clean only deterministic DB2 localization artifacts."""
    text = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'(?<=秒)秒', '', text)
    return '\n'.join(line.rstrip() for line in text.splitlines()).strip()


def _evaluate_constant_expression(expression):
    normalized = str(expression or '').replace(',', '').replace('秒', '')
    if not re.fullmatch(r'[\d.()+\-*/\s]+', normalized):
        return None
    try:
        root = ast.parse(normalized, mode='eval')
    except SyntaxError:
        return None

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        raise ValueError('unsupported expression')

    try:
        value = evaluate(root)
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return value if math.isfinite(float(value)) else None


def parse_simc_spell_query(output):
    """Parse the stable fields used by candidate tooltip rendering."""
    duration_seconds = None
    max_stacks = None
    effects = {}
    current_effect = None
    for raw_line in str(output or '').splitlines():
        line = raw_line.strip()
        duration_match = _DURATION_RE.match(line)
        if duration_match:
            duration_seconds = _number(duration_match.group('seconds'))
            continue
        stacks_match = _STACKS_RE.match(line)
        if stacks_match:
            max_stacks = _number(stacks_match.group('count'))
            continue
        header_match = _EFFECT_HEADER_RE.match(line)
        if header_match:
            current_effect = int(header_match.group('index'))
            period_match = _PERIOD_RE.search(line)
            effects[current_effect] = {
                'id': int(header_match.group('id')),
                'base_value': None,
                'scaled_value': None,
                'period_seconds': (
                    _number(period_match.group('seconds')) if period_match else None
                ),
            }
            continue
        values_match = _EFFECT_VALUES_RE.search(line)
        if values_match and current_effect is not None:
            effects[current_effect]['base_value'] = _number(values_match.group('base'))
            effects[current_effect]['scaled_value'] = _number(values_match.group('scaled'))
    return {
        'duration_seconds': duration_seconds,
        'max_stacks': max_stacks,
        'effects': effects,
    }


def _format_tooltip_number(value):
    number = float(value)
    if abs(number) >= 1000 or number.is_integer():
        return f'{round(number):,}'
    return f'{number:.2f}'.rstrip('0').rstrip('.')


def _effect_value(spell_query, effect_index):
    effect = (spell_query or {}).get('effects', {}).get(effect_index)
    if not effect:
        return None
    scaled = effect.get('scaled_value')
    if scaled not in (None, 0, 0.0):
        return scaled
    return effect.get('base_value')


def _scaled_effect_value(spell_query, effect_index):
    effect = (spell_query or {}).get('effects', {}).get(effect_index)
    if not effect:
        return None
    value = effect.get('scaled_value')
    return value if value not in (None, 0, 0.0) else None


def _effect_period(spell_query, effect_index):
    effect = (spell_query or {}).get('effects', {}).get(effect_index)
    return effect.get('period_seconds') if effect else None


def render_spell_description(
    template, *, base_spell_id, spell_queries, spell_descriptions=None, _active_spell_ids=()
):
    """Render verified numeric tokens and preserve every unsupported token."""
    text = str(template or '').replace('\r\n', '\n').replace('\r', '\n')
    spell_descriptions = spell_descriptions or {}
    unresolved = []

    def replace_spell_description(match):
        spell_id = int(match.group('spell_id'))
        if spell_id in _active_spell_ids:
            return match.group(0)
        description = spell_descriptions.get(spell_id)
        if not description:
            return match.group(0)
        rendered, nested_unresolved = render_spell_description(
            description,
            base_spell_id=spell_id,
            spell_queries=spell_queries,
            spell_descriptions=spell_descriptions,
            _active_spell_ids=(*_active_spell_ids, spell_id),
        )
        for token in nested_unresolved:
            if token not in unresolved:
                unresolved.append(token)
        return rendered

    text = re.sub(
        r'\$@spelldesc(?P<spell_id>\d+)',
        replace_spell_description,
        text,
        flags=re.IGNORECASE,
    )
    def replace_verified(match):
        groups = match.groupdict()
        spell_id = int(groups.get('spell_id') or groups.get('value_spell_id') or base_spell_id)
        kind = (groups.get('kind') or groups.get('value_kind')).lower()
        effect_index = groups.get('effect_index') or groups.get('value_effect_index')
        query = spell_queries.get(spell_id) or {}
        value = None
        if kind == 'd':
            value = query.get('duration_seconds')
            suffix = '秒'
        elif kind == 'u':
            value = query.get('max_stacks')
            suffix = ''
        elif kind == 't':
            value = _effect_period(query, int(effect_index or 1))
            suffix = ''
        elif kind == 'w':
            value = _scaled_effect_value(query, int(effect_index))
            suffix = ''
        else:
            value = _effect_value(query, int(effect_index))
            suffix = ''
        if value is None:
            return match.group(0)
        return f'{_format_tooltip_number(value)}{suffix}'

    text = _VERIFIED_TOKEN_RE.sub(replace_verified, text)

    def replace_constant_expression(match):
        value = _evaluate_constant_expression(match.group('expression'))
        if value is None:
            return match.group(0)
        return _format_tooltip_number(value)

    text = _CONSTANT_EXPRESSION_RE.sub(replace_constant_expression, text)
    for match in _UNRESOLVED_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token not in unresolved:
            unresolved.append(token)
    return normalize_tooltip_text(text), unresolved
