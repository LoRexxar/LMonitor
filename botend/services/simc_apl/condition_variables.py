"""关联最终生效的变量赋值规则；不把历史赋值替换成当前时刻的表达式。"""

from .ast import BinaryExpression, CallExpression, IdentifierExpression, UnaryExpression
from .condition_rendering import diagnostics


OPERATIONS = {'set': '赋值', 'setif': '按条件选择值', 'add': '加上', 'sub': '减去',
              'mul': '乘以', 'div': '除以', 'mod': '取余', 'pow': '乘方',
              'min': '取较小值', 'max': '取较大值', 'reset': '重置为默认值',
              'floor': '向下取整', 'ceil': '向上取整', 'print': '输出当前值'}


def identifiers(node):
    if isinstance(node, IdentifierExpression):
        yield node.name
    elif isinstance(node, BinaryExpression):
        yield from identifiers(node.left)
        yield from identifiers(node.right)
    elif isinstance(node, UnaryExpression):
        yield from identifiers(node.operand)
    elif isinstance(node, CallExpression):
        yield from identifiers(node.argument)


def collect_variables(groups):
    variables = {}
    for group in groups.values():
        for row in group['rows']:
            options = row.get('_display_options', {})
            name = options.get('name')
            if row.get('name') not in ('variable', 'cycling_variable') or not name:
                continue
            variable = variables.setdefault(name.value, {'name': name.value, 'anchor': group['anchor'],
                'rule_anchor': row['anchor'], 'definitions': []})
            op = options.get('op')
            operation = op.value if op else 'set'
            variable['definitions'].append({'anchor': group['anchor'], 'rule_anchor': row['anchor'],
                'location': f"{group['label']} · 第 {row['priority']} 项", 'line': row['line'],
                'operation': OPERATIONS.get(operation, operation),
                'cycling': row['name'] == 'cycling_variable'})
    return variables


def attach_variable_details(groups, variables):
    rows = {row['anchor']: row for g in groups.values() for row in g['rows']}
    for variable in variables.values():
        for definition in variable['definitions']:
            row = rows[definition['rule_anchor']]
            definition['fields'] = [field for field in row['fields'] if field['name'] not in ('name', 'op')]
    # 不展开表达式求值；只沿依赖关系传播“仍有未知字段”的展示状态。
    edges, own_issues = {}, {}
    for name, variable in variables.items():
        parts = [part for definition in variable['definitions'] for field in definition['fields'] for part in field['segments']]
        edges[name] = {p['variable']['name'] for p in parts if 'variable' in p}
        own_issues[name] = [dict(d, variable=name) for d in diagnostics(parts)
                           if d['reason'] in ('unknown_semantics', 'undefined_variable')]
    dependency_issues = {}
    for root in variables:
        pending, visited, issues = [root], set(), []
        while pending:
            name = pending.pop()
            if name in visited or name not in variables:
                continue
            visited.add(name)
            issues.extend(own_issues[name])
            pending.extend(sorted(edges[name] - visited))
        dependency_issues[root] = issues
    for row in rows.values():
        names = dict.fromkeys(name.removeprefix('variable.')
                             for opt in row.pop('_display_options', {}).values() if opt.expression
                             for name in identifiers(opt.expression) if name.startswith('variable.'))
        row['variables'] = [variables[name] for name in names if name in variables]
        issues = {(d['variable'], d['token'], d['reason']): d for name in names
                  for d in dependency_issues.get(name, [])}
        row['variable_diagnostics'] = list(issues.values())
        if issues:
            row['has_unexplained_conditions'] = True
