"""只依据确定的英雄天赋事实裁剪 APL，运行时条件保持未知。"""

from dataclasses import replace
from decimal import Decimal
import re

from botend.constants.hero_talents import HERO_SUBTREE_ID_TO_NAME
from .ast import BinaryExpression, IdentifierExpression, NumberExpression, UnaryExpression
from .narrative import list_title


def hero_slug(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower().replace("'", '').replace('-', '')).strip('_')


def _known_identifier(name, hero_id, symbols):
    parts = name.split('.')
    if len(parts) == 2 and parts[0] == 'hero_tree':
        found = next((sid for sid, label in HERO_SUBTREE_ID_TO_NAME.items()
                      if hero_slug(label).replace('_', '') == parts[1].replace('_', '')), None)
        return None if found is None else int(found == hero_id)
    if parts[0] == 'talent' and len(parts) in (2, 3):
        if len(parts) == 3 and parts[2] not in ('enabled', 'rank'):
            return None
        owners = (symbols.get(('talent', parts[1])) or {}).get('hero_tree_ids') or []
        # 选了某棵树不代表学习了其中所有可选节点；只排除另一棵树。
        if owners and hero_id not in owners:
            return 0
    return None


def _number(node):
    return Decimal(node.value) if isinstance(node, NumberExpression) else None


def _literal(value, node):
    return NumberExpression(str(int(value)), node.range)


def simplify_hero_condition(node, hero_id, symbols, boolean_context=True):
    if isinstance(node, IdentifierExpression):
        known = _known_identifier(node.name, hero_id, symbols)
        return node if known is None else _literal(known, node)
    if isinstance(node, UnaryExpression):
        operand = simplify_hero_condition(node.operand, hero_id, symbols, node.operator == '!')
        value = _number(operand)
        if node.operator == '!' and value is not None:
            return _literal(not value, node)
        return replace(node, operand=operand)
    if not isinstance(node, BinaryExpression):
        return node
    logical = node.operator in ('&', '|')
    left = simplify_hero_condition(node.left, hero_id, symbols, logical)
    right = simplify_hero_condition(node.right, hero_id, symbols, logical)
    a, b = _number(left), _number(right)
    if logical and a is not None and b is not None:
        return _literal(bool(a) and bool(b) if node.operator == '&' else bool(a) or bool(b), node)
    if node.operator == '&':
        if a == 0 or b == 0:
            return _literal(0, node)
        if boolean_context and a is not None:
            return right
        if boolean_context and b is not None:
            return left
    if node.operator == '|':
        if a is not None and a != 0 or b is not None and b != 0:
            return _literal(1, node)
        if boolean_context and a == 0:
            return right
        if boolean_context and b == 0:
            return left
    if a is not None and b is not None:
        comparisons = {'=': a == b, '!=': a != b, '>': a > b, '<': a < b,
                       '>=': a >= b, '<=': a <= b}
        if node.operator in comparisons:
            return _literal(comparisons[node.operator], node)
    return replace(node, left=left, right=right)


def filter_options(options, hero_id, symbols):
    check = options.get('if')
    if not hero_id or not check or not check.expression:
        return options
    expression = simplify_hero_condition(check.expression, hero_id, symbols)
    known = _number(expression)
    if known == 0:
        return None
    result = dict(options)
    if known is not None:
        result.pop('if')
    else:
        result['if'] = replace(check, expression=expression)
    return result


def scenario_title(name, hero_id):
    if not hero_id or name is None:
        return list_title(name)
    slug = hero_slug(HERO_SUBTREE_ID_TO_NAME.get(hero_id, ''))
    # 这些缩写来自上游对应英雄树的分流列表；只在选定该树后剥离。
    aliases = {'mountain_thane': ('thane',), 'sunfury': ('sf',), 'frostfire': ('ff',)}
    for prefix in (slug, *aliases.get(slug, ())):
        if prefix and name == prefix:
            return '常规优先级'
        if prefix and name.startswith(prefix + '_'):
            return list_title(name[len(prefix) + 1:])
    return list_title(name)


def reachable_groups(groups, original_targets):
    """从原始入口出发，删除筛选后不可达的组；不按列表名称猜测归属。"""
    roots = {key for key in groups if key in (None, 'precombat') or key not in original_targets}
    if not roots or any(r.get('unsupported') for g in groups.values() for r in g['rows']):
        return groups
    reachable, pending = set(), list(roots)
    while pending:
        key = pending.pop()
        if key in reachable or key not in groups:
            continue
        reachable.add(key)
        pending.extend(row['target'] for row in groups[key]['rows'] if row.get('target') is not None)
    return {key: group for key, group in groups.items() if key in reachable}
