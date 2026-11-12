"""按最终调用图组织优先级层级；文件中的定义顺序不是执行顺序。"""

import re

from .ast import IdentifierExpression, UnaryExpression


def _buff_phase(expression, symbols, inverse=False):
    """只从可确认的单一状态谓词命名阶段，不由 meta/out 等缩写猜测。"""
    while isinstance(expression, UnaryExpression) and expression.operator == '!':
        expression, inverse = expression.operand, not inverse
    if not isinstance(expression, IdentifierExpression):
        return None
    parts = expression.name.split('.')
    if len(parts) != 3 or parts[0] != 'buff' or parts[2] not in ('up', 'down'):
        return None
    spell = symbols.get(('buff', parts[1]))
    if not spell or not spell.get('name') or spell['name'] == parts[1]:
        return None
    active = (parts[2] == 'up') != inverse
    return spell['name'] + ('期间' if active else '之外')


def label_groups(groups, symbols):
    incoming = {name: [] for name in groups}
    for group in groups.values():
        previous = []
        for row in group['rows']:
            if row.get('target') in groups and row.get('target') is not None:
                incoming[row['target']].append((row, list(previous)))
            if row.get('name') == 'run_action_list':
                previous.append(row)
    for name, group in groups.items():
        rows = group['rows']
        group['dispatch_only'] = bool(rows) and all(r.get('target') is not None for r in rows)
        group['setup_only'] = bool(rows) and all(r.get('name') in ('variable', 'cycling_variable', 'snapshot_stats') for r in rows)
        if name in (None, 'precombat'):
            continue
        if group['label'] == '常规优先级' and group['dispatch_only']:
            group['label'] = '阶段分流' if all(r['name'] == 'run_action_list' for r in rows) else '优先级调度'
        if re.fullmatch(r'[a-z0-9_]+', group['label']) and 0 < len(rows) <= 3:
            names = [r.get('reference', {}).get('name') if r.get('reference') else None for r in rows]
            if all(label and label != row.get('name') for label, row in zip(names, rows)):
                group['label'] = ' → '.join(names)
        if len(incoming[name]) == 1:
            row, previous = incoming[name][0]
            options = row.get('_routing_options', {})
            check = options.get('if')
            phase = None
            if not set(options) - {'name', 'if'}:
                if check and not previous:
                    phase = _buff_phase(check.expression, symbols)
                elif not check and len(previous) == 1:
                    prev_options = previous[0].get('_routing_options', {})
                    prev_check = prev_options.get('if')
                    if previous[0].get('target') in groups and prev_check and not set(prev_options) - {'name', 'if'}:
                        phase = _buff_phase(prev_check.expression, symbols, inverse=True)
            if phase:
                group['label'] = phase


def build_hierarchy(groups):
    """每组只展示一次；多入口组独立列出，调用位置留在父列表中。"""
    children = {name: [] for name in groups}
    shared = []
    for name, group in groups.items():
        entries = group['entries']
        if name is None:
            role = '主优先级'
        elif name == 'precombat':
            role = '开战准备'
        elif len(entries) > 1:
            role = '公共子优先级'
            shared.append(name)
        elif group['setup_only']:
            role = '条件准备'
        elif group['dispatch_only']:
            role = '条件分流'
        elif entries:
            role = '接管优先级' if entries[0]['kind'] == 'run_action_list' else '局部优先级'
        else:
            role = '未关联列表'
        group['role_label'] = role
        if len(entries) == 1:
            entry = entries[0]
            parts = [guard['text'] for guard in entry['guards']]
            if not entry['unconditional']:
                parts.append(entry['text'])
            group['trigger_hint'] = '；'.join(parts)
        if len(entries) == 1 and name not in (None, 'precombat'):
            entry = entries[0]
            children[entry['parent_name']].append((entry['priority'], name))
    for rows in children.values():
        rows.sort(key=lambda item: item[0])

    sections, seen = [], set()

    def section(title, roots):
        items = []

        def visit(name, depth=0, path=''):
            if name in seen:
                return
            seen.add(name)
            group = groups[name]
            group['hierarchy_path'] = path
            group['hierarchy_section'] = title
            items.append({'group': group, 'depth': min(depth, 3), 'level': depth + 1, 'path': path})
            for priority, child in children[name]:
                visit(child, depth + 1, f'{path}.{priority}' if path else str(priority))

        for root in roots:
            visit(root)
        if items:
            sections.append({'title': title, 'items': items})

    section('战斗优先级', [None] if None in groups else [])
    section('公共子优先级', shared)
    section('开战前', ['precombat'] if 'precombat' in groups else [])
    section('未关联或循环调用', [name for name in groups if name not in seen])

    # 仅展开一层真实分流，让主列表能看到下一级决策，不复制整棵调用图。
    for group in groups.values():
        for row in group['rows']:
            target = groups.get(row.get('target')) if row.get('target') is not None else None
            if target is None:
                continue
            row['target_role'] = target['role_label']
            if target['dispatch_only']:
                row['branches'] = [{
                    'priority': child['priority'], 'label': child['target_label'],
                    'anchor': child.get('target_anchor'), 'condition': child['entry_condition'],
                    'guards': child['entry_guards'], 'kind': child['name'],
                    'extra': child['entry_extra'], 'behavior': child['behavior'],
                    'unconditional': child['entry_unconditional'],
                } for child in target['rows']]
    return sections
