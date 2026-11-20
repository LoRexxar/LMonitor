"""将已知布尔计算整理为决策分支；保留数值比较边界和变量时序。"""

from collections import Counter
from dataclasses import replace
from decimal import Decimal

from .ast import BinaryExpression, CallExpression, IdentifierExpression, NumberExpression, UnaryExpression


COMPARISONS = ('=', '!=', '<', '<=', '>', '>=')
INVERSE = {'=': '!=', '!=': '=', '<': '>=', '<=': '>', '>': '<=', '>=': '<'}


def key(node):
    """只比较表达式身份，忽略原始位置。"""
    if isinstance(node, IdentifierExpression):
        return ('id', node.name)
    if isinstance(node, NumberExpression):
        return ('number', Decimal(node.value))
    if isinstance(node, UnaryExpression):
        return (node.operator, key(node.operand))
    if isinstance(node, BinaryExpression):
        return (node.operator, key(node.left), key(node.right))
    if isinstance(node, CallExpression):
        return ('call', node.function, key(node.argument))
    return None


def opposite(node):
    if isinstance(node, UnaryExpression) and node.operator == '!':
        return node.operand
    if isinstance(node, BinaryExpression) and node.operator in COMPARISONS:
        return replace(node, operator=INVERSE[node.operator])
    return UnaryExpression('!', node, node.range)


def number(value, node):
    return NumberExpression(str(int(value)) if float(value).is_integer() else repr(float(value)), node.range)


def simplify(node, *, boolean=False):
    """仅折叠常量和布尔恒等式，不重排浮点计算或消去未知数值项。"""
    if isinstance(node, UnaryExpression):
        operand = simplify(node.operand, boolean=node.operator == '!')
        if isinstance(operand, NumberExpression):
            value = float(operand.value)
            if node.operator in ('!', '+', '-'):
                return number({'!': float(not value), '+': value, '-': -value}[node.operator], node)
        return replace(node, operand=operand)
    if not isinstance(node, BinaryExpression):
        return node
    logical = node.operator in ('&', '|', '^')
    left = simplify(node.left, boolean=logical)
    right = simplify(node.right, boolean=logical)
    node = replace(node, left=left, right=right)
    if isinstance(left, NumberExpression) and isinstance(right, NumberExpression):
        a, b = float(left.value), float(right.value)
        operations = {'+': lambda: a + b, '-': lambda: a - b, '*': lambda: a * b,
                      '=': lambda: a == b, '!=': lambda: a != b, '<': lambda: a < b,
                      '<=': lambda: a <= b, '>': lambda: a > b, '>=': lambda: a >= b,
                      '&': lambda: bool(a) and bool(b), '|': lambda: bool(a) or bool(b),
                      '^': lambda: bool(a) != bool(b)}
        if node.operator in operations:
            result = operations[node.operator]()
            if abs(result) != float('inf'):
                return number(result, node)
    if node.operator in ('&', '|'):
        for constant, other in ((left, right), (right, left)):
            if not isinstance(constant, NumberExpression):
                continue
            enabled = bool(float(constant.value))
            if (node.operator == '&' and not enabled) or (node.operator == '|' and enabled):
                return number(enabled, node)
            # 数值上下文仍需产生 0/1，不能将数值字段直接作为逻辑结果返回。
            return other if boolean else UnaryExpression('!', UnaryExpression('!', other, other.range), other.range)
    if node.operator == '*':
        if isinstance(left, NumberExpression) and float(left.value) == 1:
            return right
        if isinstance(right, NumberExpression) and float(right.value) == 1:
            return left
    return node


def restrict(node, predicate, enabled, *, boolean=True):
    wanted, inverse = key(predicate), key(opposite(predicate))
    def visit(value):
        identity = key(value)
        if identity == wanted:
            return number(enabled, value)
        if identity == inverse:
            return number(not enabled, value)
        if isinstance(value, UnaryExpression):
            return replace(value, operand=visit(value.operand))
        if isinstance(value, BinaryExpression):
            return replace(value, left=visit(value.left), right=visit(value.right))
        if isinstance(value, CallExpression):
            return replace(value, argument=visit(value.argument))
        return value
    return simplify(visit(node), boolean=boolean)


def boolean_atom(node, symbols):
    if isinstance(node, BinaryExpression) and node.operator in (*COMPARISONS, '&', '|', '^'):
        return True
    if isinstance(node, UnaryExpression) and node.operator == '!':
        return True
    if isinstance(node, IdentifierExpression):
        from .narrative import _predicate, _quantity
        # 数值型 remains、层数、变量不能因为能充当条件就假定值为 0/1。
        return _quantity(node.name, symbols) is None and _predicate(node.name, symbols) is not None
    return False


def conditional_terms(node, symbols):
    """只收集算式内真正产生 0/1 的项。"""
    if boolean_atom(node, symbols):
        yield node
    elif isinstance(node, BinaryExpression) and node.operator in ('+', '-', '*'):
        yield from conditional_terms(node.left, symbols)
        yield from conditional_terms(node.right, symbols)


def decision(node, symbols):
    """返回一个穷尽且互斥的二分；不会展开成指数数量的场景。"""
    # 条件参与数值门槛时，将 0/1 运算改为两个具体门槛。
    def gates(value):
        if isinstance(value, UnaryExpression) and value.operator == '!':
            yield from gates(value.operand)
        elif isinstance(value, BinaryExpression) and value.operator in ('&', '|'):
            yield from gates(value.left)
            yield from gates(value.right)
        elif isinstance(value, BinaryExpression) and value.operator in COMPARISONS:
            yield from (term for side in (value.left, value.right)
                        if isinstance(side, BinaryExpression) and side.operator in ('+', '-', '*')
                        for term in conditional_terms(side, symbols))
    terms = list(gates(node))
    if terms:
        # 比较本身不是算式中的门槛项；只处理一个独立布尔条件，避免分支爆炸。
        unique = {key(term): term for term in terms}
        if len(unique) == 1:
            predicate = next(iter(unique.values()))
            yes, no = restrict(node, predicate, True), restrict(node, predicate, False)
            if key(yes) != key(no):
                return predicate, yes, no

    # 同一目标数／天赋判定出现在多个逻辑分支时，按它拆成互斥场景。
    candidates, counts = {}, Counter()
    def collect(value):
        if isinstance(value, BinaryExpression) and value.operator in ('&', '|'):
            collect(value.left)
            collect(value.right)
        elif isinstance(value, UnaryExpression) and value.operator == '!':
            collect(value.operand)
        elif isinstance(value, BinaryExpression) and value.operator in ('=', '!='):
            if isinstance(value.left, IdentifierExpression) and value.left.name == 'active_enemies' and isinstance(value.right, NumberExpression):
                candidate = replace(value, operator='=')
                candidates[key(candidate)] = candidate
                counts[key(candidate)] += 1
        elif (isinstance(value, IdentifierExpression) and value.name.startswith(('talent.', 'hero_tree.'))
              and boolean_atom(value, symbols)):
            candidates[key(value)] = value
            counts[key(value)] += 1
    collect(node)
    for identity, count in counts.most_common():
        if count < 2:
            break
        predicate = candidates[identity]
        yes, no = restrict(node, predicate, True), restrict(node, predicate, False)
        if key(yes) != key(no):
            return predicate, yes, no
    return None
