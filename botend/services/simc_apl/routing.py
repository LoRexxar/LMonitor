"""还原列表调用的进入门槛和前置转入规则，不把子列表误当成互斥场景。"""

from .ast import BinaryExpression, CallExpression, IdentifierExpression, NumberExpression, UnaryExpression
from .narrative import condition, text


def expression_source(node):
    """按语法树保留未知条件，括号保证组合含义不变。"""
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


def route_condition(node, symbols, negate=False):
    known = condition(node, symbols, negate)
    if known is not None:
        return known
    if isinstance(node, UnaryExpression) and node.operator == '!':
        return route_condition(node.operand, symbols, not negate)
    if isinstance(node, BinaryExpression) and node.operator in ('&', '|'):
        op = node.operator if not negate else ('|' if node.operator == '&' else '&')
        return [text('（'), *route_condition(node.left, symbols, negate),
                text('，并且' if op == '&' else '，或者'),
                *route_condition(node.right, symbols, negate), text('）')]
    return [{'raw': expression_source(node)}, text('不成立' if negate else '成立')]


def route_text(segments):
    return ''.join(s['reference']['name'] if 'reference' in s else s.get('raw', s.get('text', '')) for s in segments)


def annotate_routes(groups, symbols):
    """只由真实调用边构建入口；call 不构成后续分支的互斥条件。"""
    for group in groups.values():
        group['entries'] = []
    for name, group in groups.items():
        previous_runs = []
        for row in group['rows']:
            options = row.pop('_routing_options', None)
            if options is None:
                continue
            check = options.get('if')
            own = route_condition(check.expression, symbols) if check and check.expression else [text('到达此项时检查，无额外条件')]
            guards = []
            blocked = False
            for previous, previous_options in previous_runs:
                prev_check = previous_options.get('if')
                # line_cd、sync 等设置可能改变是否转入，不能仅反转 if 就当成完整门槛。
                if set(previous_options) - {'name', 'if'} or not previous.get('target_anchor'):
                    parts = [text('前面的转入规则未进入；需结合该规则的执行设置')]
                elif prev_check and prev_check.expression:
                    parts = route_condition(prev_check.expression, symbols, True)
                else:
                    parts = [text('前面的无条件转入已接管，此路径不会到达本项')]
                    blocked = True
                guards.append({'condition': parts, 'text': route_text(parts), 'priority': previous['priority'],
                               'rule_anchor': previous['anchor'], 'anchor': group['anchor'],
                               'target_label': previous['target_label']})
            row.update(entry_condition=own, entry_text=route_text(own), entry_guards=guards,
                       entry_blocked=blocked, entry_extra=bool(set(options) - {'name', 'if'}),
                       entry_unconditional=check is None)
            target = groups.get(row.get('target'))
            if target is not None:
                target['entries'].append({
                    'parent': group['label'], 'parent_name': name, 'anchor': group['anchor'],
                    'rule_anchor': row['anchor'], 'priority': row['priority'], 'line': row['line'],
                    'condition': own, 'text': row['entry_text'], 'guards': guards,
                    'blocked': blocked, 'extra': row['entry_extra'], 'behavior': row['behavior'],
                    'kind': row['name'],
                    'unconditional': check is None,
                    'target_label': target['label'],
                })
            if row['name'] == 'run_action_list':
                previous_runs.append((row, options))

    for name, group in groups.items():
        paths, incomplete, visits = [], False, 0

        def visit(current, suffix, visited):
            nonlocal incomplete, visits
            visits += 1
            if visits > 256 or len(paths) >= 24 or len(visited) > 12 or current in visited:
                incomplete = True
                return
            if current in (None, 'precombat'):
                paths.append(suffix)
                return
            incoming = groups[current]['entries']
            if not incoming:
                incomplete = True
            for edge in incoming:
                if visits >= 256:
                    incomplete = True
                    break
                visit(edge['parent_name'], [edge, *suffix], visited | {current})

        if name not in (None, 'precombat'):
            visit(name, [], set())
        group.update(entry_paths=paths, paths_incomplete=incomplete)
        entries = group['entries']
        if name is None:
            hint = '每次选择行动的起点'
        elif name == 'precombat':
            hint = '开战前准备'
        elif len(entries) == 1:
            entry = entries[0]
            hint = '；'.join(g['text'] for g in entry['guards']) or entry['text']
            if entry['guards'] and entry['text'] != '到达此项时检查，无额外条件':
                hint += '；' + entry['text']
            hint = f"{entry['parent']}第 {entry['priority']} 项：{hint}"
        elif entries:
            hint = f'{len(entries)} 处调用，各入口条件分别列出'
        else:
            hint = '未找到入口，不推定执行场景'
        group['nav_hint'] = hint
