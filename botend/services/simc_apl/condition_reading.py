"""面向读者的条件说明：使用场景、动态门槛以及效果状态。"""

from decimal import Decimal

from .ast import BinaryExpression, IdentifierExpression, NumberExpression, UnaryExpression
from .condition_decisions import boolean_atom, decision, opposite
from .condition_terms import contextual_name
from .narrative import COMPARISONS, _quantity, _spell, text


def _numeric_truth(node):
    return bool(Decimal(node.value)) if isinstance(node, NumberExpression) else None


def decision_reading(node, symbols, render):
    split = decision(node, symbols)
    if split is None:
        return None
    predicate, yes, no = split
    positive, negative = render(predicate), render(opposite(predicate))
    # 单一分支不成立时就是普通的必要条件，不需要展示空分支。
    if _numeric_truth(yes) is False:
        return [*negative, text('，并且（'), *render(no), text('）')]
    if _numeric_truth(no) is False:
        return [*positive, text('，并且（'), *render(yes), text('）')]
    parts = [text('按情况判断：①'), *positive, text('时，')]
    parts += [text('此项条件已满足')] if _numeric_truth(yes) is True else render(yes)
    parts += [text('；②'), *negative, text('时，')]
    parts += [text('此项条件已满足')] if _numeric_truth(no) is True else render(no)
    return parts


def threshold_reading(node, symbols, render):
    """常量基准加多个条件增减量，直接说明门槛如何调整，不列出所有组合。"""
    if not isinstance(node, BinaryExpression) or node.operator not in COMPARISONS:
        return None
    if not isinstance(node.left, IdentifierExpression):
        return None
    quantity = _quantity(node.left.name, symbols)
    if quantity is None:
        return None
    base, changes = Decimal(0), []
    def collect(value, coefficient=Decimal(1)):
        nonlocal base
        if isinstance(value, NumberExpression):
            numeric = Decimal(value.value)
            if numeric != numeric.to_integral() or abs(numeric) > 2 ** 30:
                return False
            base += coefficient * numeric
            return True
        if boolean_atom(value, symbols):
            changes.append((value, coefficient))
            return True
        if isinstance(value, BinaryExpression):
            if value.operator in ('+', '-'):
                return collect(value.left, coefficient) and collect(value.right, coefficient * (-1 if value.operator == '-' else 1))
            if value.operator == '*':
                for constant, other in ((value.left, value.right), (value.right, value.left)):
                    if isinstance(constant, NumberExpression) and boolean_atom(other, symbols):
                        return collect(other, coefficient * Decimal(constant.value))
        return False
    if not collect(node.right) or len(changes) < 2:
        return None
    # 非整数常量不重排，避免浮点运算次序改变临界值。
    if any(value != value.to_integral() or abs(value) > 2 ** 30 for value in (base, *(c for _, c in changes))):
        return None
    label, unit = quantity
    parts = [*label, text(COMPARISONS[node.operator] + f'动态门槛（基础为{int(base)}{unit}')]
    for predicate, amount in changes:
        parts += [text('；'), *render(predicate), text(f'时{"增加" if amount >= 0 else "降低"}{abs(int(amount))}{unit}')]
    return [*parts, text('；各项调整可叠加）')]


def state_reading(node, symbols, negate=False):
    """只替换已确认的非负时长与层数的零边界，不猜测变量的含义。"""
    if isinstance(node, UnaryExpression) and node.operator == '!':
        return state_reading(node.operand, symbols, not negate)
    if isinstance(node, IdentifierExpression):
        name, present = node.name, not negate
    elif (isinstance(node, BinaryExpression) and isinstance(node.left, IdentifierExpression)
          and isinstance(node.right, NumberExpression) and Decimal(node.right.value) == 0):
        if node.operator not in ('=', '!=', '>', '<='):
            return None
        name = node.left.name
        present = (node.operator in ('!=', '>')) != negate
    else:
        return None
    parts = contextual_name(name, symbols).split('.')
    if len(parts) != 3:
        return None
    kind, token, prop = parts
    if kind in ('buff', 'debuff', 'dot') and prop == 'remains':
        # 原生永久增益的 remains 使用负哨兵；>0 和 !=0 不能互换。
        if kind in ('buff', 'debuff') and isinstance(node, BinaryExpression) and node.operator in ('>', '<='):
            return None
        ref = _spell(kind, token, symbols)
        if ref:
            return [text('目标的' if kind in ('debuff', 'dot') else ''), *ref,
                    text('效果仍在持续' if present else '效果已不在持续')]
    if kind == 'cooldown' and prop == 'remains':
        ref = _spell(kind, token, symbols)
        if ref:
            return [*ref, text('仍在冷却' if present else '已冷却完毕')]
    return None
