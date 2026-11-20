"""无遗漏的表达式说明：未知叶子仍显示原身份，不丢弃相邻的已知条件。"""

from .ast import BinaryExpression, CallExpression, IdentifierExpression, NumberExpression, UnaryExpression
from .narrative import condition, _amount, _logical_operator, COMPARISONS, INVERSE, text, simplify_boolean_comparison
from .condition_reading import decision_reading, threshold_reading, state_reading
from .condition_decisions import opposite


def expression_source(node):
    if isinstance(node, IdentifierExpression):
        return node.name
    if isinstance(node, NumberExpression):
        return node.value
    if isinstance(node, UnaryExpression):
        return f'{node.operator}({expression_source(node.operand)})'
    if isinstance(node, BinaryExpression):
        return f'({expression_source(node.left)}{node.operator}{expression_source(node.right)})'
    if isinstance(node, CallExpression):
        return f'{node.function}({expression_source(node.argument)})'
    return ''


def unresolved(node):
    return {'raw': expression_source(node), 'reason': 'unknown_semantics'}


def describe_amount(node, symbols):
    known = _amount(node, symbols)
    if known is not None:
        return known
    if isinstance(node, IdentifierExpression):
        reason = 'undefined_variable' if node.name.startswith('variable.') else 'unknown_semantics'
        return [{'raw': node.name, 'reason': reason}]
    if isinstance(node, BinaryExpression):
        if node.operator in (*COMPARISONS, '&', '|', '^'):
            return [text('（'), *describe_condition(node, symbols), text('时取1，否则取0）')]
        words = {'+': '加上', '-': '减去', '*': '乘以', '%': '除以', '%%': '取余于'}
        left, right = describe_amount(node.left, symbols), describe_amount(node.right, symbols)
        if node.operator in ('<?', '>?'):
            return [text('（'), *left, text('与'), *right,
                    text('中的较大值）' if node.operator == '<?' else '中的较小值）')]
        if node.operator in words:
            return [text('（'), *left, text(words[node.operator]), *right, text('）')]
    if isinstance(node, UnaryExpression):
        if node.operator == '!':
            return [text('（'), *describe_condition(node, symbols), text('时取1，否则取0）')]
        if node.operator in ('+', '-', '@'):
            value = describe_amount(node.operand, symbols)
            return value if node.operator == '+' else [text('（'), *value,
                text('的相反数）' if node.operator == '-' else '的绝对值）')]
    if isinstance(node, CallExpression) and node.function in ('floor', 'ceil'):
        return [text('（'), *describe_amount(node.argument, symbols),
                text('向下取整）' if node.function == 'floor' else '向上取整）')]
    return [unresolved(node)]


def describe_condition(node, symbols, negate=False, *, decisions=True):
    node = simplify_boolean_comparison(node)
    render = lambda value: describe_condition(value, symbols, decisions=False)
    if decisions:
        reading = decision_reading(opposite(node) if negate else node, symbols, render)
        if reading is not None:
            return reading
    reading = state_reading(node, symbols, negate)
    if reading is not None:
        return reading
    if isinstance(node, UnaryExpression) and node.operator == '!':
        return describe_condition(node.operand, symbols, not negate, decisions=False)
    if isinstance(node, BinaryExpression):
        op = node.operator
        if op in ('&', '|'):
            op = _logical_operator(node, negate)
            children = []
            for child in (node.left, node.right):
                parts = describe_condition(child, symbols, negate, decisions=False)
                child_op = _logical_operator(child, negate)
                if child_op and child_op != op:
                    parts = [text('（'), *parts, text('）')]
                children.append(parts)
            return [*children[0], text('，并且' if op == '&' else '，或者'), *children[1]]
    reading = threshold_reading(opposite(node) if negate else node, symbols, render)
    if reading is not None:
        return reading
    known = condition(node, symbols, negate)
    if known is not None:
        return known
    if isinstance(node, BinaryExpression):
        op = node.operator
        if op == '^':
            return [text('以下两个条件' + ('同时成立或同时不成立：' if negate else '恰好一个成立：')),
                    text('（'), *describe_condition(node.left, symbols), text('）；（'),
                    *describe_condition(node.right, symbols), text('）')]
        if op in COMPARISONS:
            op = INVERSE[op] if negate else op
            return [*describe_amount(node.left, symbols), text(COMPARISONS[op]), *describe_amount(node.right, symbols)]
        if op not in ('+', '-', '*', '%', '%%', '<?', '>?'):
            return [unresolved(node), text('不成立' if negate else '成立')]
    return [*describe_amount(node, symbols), text('为0' if negate else '不为0')]


def diagnostics(segments):
    """结构化列出待解释字段，供界面和全量审计共用。"""
    found = {}
    for part in segments:
        if 'raw' in part:
            reason = part.get('reason', 'unknown_semantics')
            found.setdefault((reason, part['raw']), {'reason': reason, 'token': part['raw']})
    return list(found.values())
