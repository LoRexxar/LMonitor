"""将 APL 解析为玩家可阅读的优先级，保留条件、列表重置与未知语法。"""

from collections import OrderedDict

from botend.constants.wow import canonical_class_spec
from botend.models import SimcAplSymbolScope, WowTalentNodeMetadata
from botend.services.simc_apl.ast import (
    ActionAssignment, ActionEntry, BinaryExpression, CallExpression,
    IdentifierExpression, InvalidLine, NumberExpression, Option, UnaryExpression,
)
from botend.services.simc_apl.catalog import query_visible_symbols
from botend.services.simc_apl.parser import parse
from botend.services.simc_apl.narrative import action_guide, list_title
from botend.services.simc_apl.hero_filter import filter_options, reachable_groups, scenario_title
from botend.services.wow_spell_display import SpellDisplayReader
from botend.templatetags.wow_tags import wow_icon_oss_url


CONTROL_LABELS = {
    'call_action_list': '检查子优先级', 'run_action_list': '转入子优先级',
    'variable': '设置变量', 'cycling_variable': '按目标设置变量',
    'pool_resource': '等待资源', 'wait': '等待', 'auto_attack': '开始自动攻击',
    'snapshot_stats': '记录战斗开始属性', 'potion': '使用药水',
    'use_item': '使用装备', 'use_items': '使用可用装备',
    'invoke_external_buff': '请求外部增益', 'cancel_buff': '取消增益',
    'retarget_auto_attack': '切换自动攻击目标',
}
LIST_LABELS = {
    None: '战斗优先级', 'precombat': '战斗准备', 'cooldowns': '爆发技能',
    'trinkets': '饰品', 'variables': '条件变量', 'aoe': '多目标',
    'single_target': '单目标', 'st': '单目标', 'cleave': '顺劈',
}
OPTION_LABELS = {
    'if': '使用条件', 'target_if': '目标选择', 'interrupt_if': '中断引导条件',
    'cancel_if': '取消条件', 'early_chain_if': '提前衔接条件',
    'name': '名称', 'slot': '装备槽位', 'value': '数值', 'value_else': '否则取值',
    'condition': '判断条件', 'op': '运算', 'sec': '等待秒数',
    'cycle_targets': '遍历目标', 'max_cycle_targets': '最多遍历目标数',
    'moving': '移动要求', 'for_next': '为下一项保留资源', 'extra_amount': '额外资源',
    'interrupt': '允许中断引导', 'chain': '连续引导', 'toggle': '启用状态',
    'line_cd': '此优先级检查间隔', 'sync': '同步技能', 'target': '目标',
}
VALUE_LABELS = {
    'rage': '怒气', 'energy': '能量', 'focus': '集中值', 'mana': '法力',
    'runic_power': '符文能量', 'rune': '符文', 'holy_power': '圣能',
    'chi': '真气', 'combo_points': '连击点', 'essence': '精华',
    'soul_shard': '灵魂碎片', 'fury': '恶魔之怒', 'insanity': '狂乱值',
    'active_enemies': '敌人数量', 'time': '战斗时间', 'fight_remains': '战斗剩余时间',
    'target.time_to_die': '目标预计存活时间', 'target.health.pct': '目标生命百分比',
    'gcd': '公共冷却时间', 'gcd.max': '最大公共冷却时间',
    'gcd.remains': '公共冷却剩余时间', 'movement.distance': '移动距离',
    'true': '是', 'false': '否',
}
PROPERTIES = {
    'up': '生效', 'down': '未生效', 'remains': '剩余时间', 'duration': '持续时间',
    'stack': '层数', 'stacks': '层数', 'react': '可响应层数', 'ready': '就绪',
    'enabled': '已学习', 'rank': '等级', 'ticking': '持续效果生效',
    'refreshable': '可刷新', 'charges': '充能次数', 'charges_fractional': '充能进度',
    'full_recharge_time': '全部充能恢复时间',
}
OPERATORS = {'&': '且', '|': '或', '^': '异或', '=': '=', '!=': '≠', '>=': '≥', '<=': '≤', '%': '÷', '%%': '取余'}


def display_symbols(class_name, spec_name, talent_version=None, *, branch='retail'):
    """仅按已验证的 token 类型和 ID 关联提示，不用名字猜测技能或天赋。"""
    hero_trees = list(SimcAplSymbolScope.objects.filter(
        class_name=class_name, spec=spec_name, is_active=True, symbol__is_active=True,
    ).exclude(hero_tree__isnull=True).exclude(hero_tree='').values_list('hero_tree', flat=True).distinct())
    candidates = {}
    for hero in [None, *hero_trees]:
        for row in query_visible_symbols(class_name, spec_name, hero):
            candidates.setdefault((row.symbol_kind, row.token), {})[row.pk] = row
    symbols = {}
    for key, rows in candidates.items():
        identities = {(r.spell_id, r.trait_id) for r in rows.values()}
        if len(identities) == 1:
            symbols[key] = next(iter(rows.values()))
    trait_ids = {r.trait_id for r in symbols.values() if r.symbol_kind == 'talent' and r.trait_id}
    traits = {}
    if talent_version and trait_ids:
        canonical_class, canonical_spec = canonical_class_spec(class_name, spec_name) or (class_name, spec_name)
        rows = WowTalentNodeMetadata.objects.filter(
            talent_version=talent_version, class_name__iexact=canonical_class,
            spec_name__iexact=canonical_spec, node_id__in=trait_ids,
        )
        for row in rows:
            traits[row.node_id] = row
    reader = SpellDisplayReader(talent_version.branch if talent_version else branch)
    reader.preload({r.spell_id for r in symbols.values() if r.spell_id} | {
        r.display_spell_id or r.spell_id for r in traits.values() if r.display_spell_id or r.spell_id
    })
    result = {}
    for key, row in symbols.items():
        trait = traits.get(row.trait_id) if row.symbol_kind == 'talent' else None
        # TraitNodeEntry ID 与技能 ID 不是同一命名空间。
        sid = (trait.display_spell_id or trait.spell_id) if trait else (row.spell_id if row.symbol_kind != 'talent' else None)
        data = reader.get(sid, class_name=class_name, spec_name=spec_name,
                          node_id=trait.node_id if trait else None) if sid else {}
        name = data.get('name_zh') or (trait.name_zh if trait else '') or row.name_zh or data.get('name') or row.name_en or row.token
        description = data.get('description_zh') or data.get('description') or (trait.description_zh or trait.description if trait else '')
        if row.symbol_kind in ('buff', 'debuff', 'dot'):
            description = data.get('aura_description_zh') or data.get('aura_description') or description
        icon = data.get('icon') or (trait.icon if trait else '')
        result[key] = {
            'name': name, 'token': row.token, 'spell_id': sid, 'kind': row.symbol_kind,
            'icon': wow_icon_oss_url(icon) if icon else '',
            'description': description or '当前资料尚未提供说明。',
            'hero_tree_ids': [trait.db2_subtree_id] if trait and trait.db2_subtree_id else [],
        }
    return result


def _text(value):
    return {'text': str(value)}


def _identifier(name, symbols):
    if name in VALUE_LABELS:
        return [_text(VALUE_LABELS[name])]
    parts = name.split('.')
    if len(parts) >= 2 and parts[0] in ('talent', 'buff', 'debuff', 'dot', 'cooldown', 'action'):
        kind, token = parts[:2]
        item = symbols.get((kind, token))
        # 冷却字段确实属于一个动作时，才允许用动作的已验证映射补充显示。
        if item is None and kind == 'cooldown':
            item = symbols.get(('action', token))
        prefix = {'talent': '天赋', 'buff': '增益', 'debuff': '减益', 'dot': '持续伤害', 'cooldown': '冷却', 'action': '技能'}[kind]
        suffix = ' · '.join(PROPERTIES.get(p, p) for p in parts[2:])
        return [_text(prefix + ' '), {'reference': item} if item else _text(token), _text(' ' + suffix if suffix else '')]
    if name.startswith('variable.'):
        return [_text('变量 ' + name.removeprefix('variable.'))]
    for resource, label in VALUE_LABELS.items():
        if name.startswith(resource + '.'):
            suffix = name[len(resource) + 1:]
            return [_text(label + ' · ' + {'deficit': '缺口', 'max': '上限', 'pct': '百分比', 'regen': '每秒恢复', 'time_to_max': '恢复至上限时间'}.get(suffix, suffix))]
    return [_text(name)]


def _expression(node, symbols):
    if isinstance(node, IdentifierExpression):
        return _identifier(node.name, symbols)
    if isinstance(node, NumberExpression):
        return [_text(node.value)]
    if isinstance(node, UnaryExpression):
        return [_text({'!': '非 ', '@': '绝对值 '}.get(node.operator, node.operator) + '('), *_expression(node.operand, symbols), _text(')')]
    if isinstance(node, BinaryExpression):
        return [_text('('), *_expression(node.left, symbols), _text(' ' + OPERATORS.get(node.operator, node.operator) + ' '), *_expression(node.right, symbols), _text(')')]
    if isinstance(node, CallExpression):
        return [_text({'floor': '向下取整', 'ceil': '向上取整'}.get(node.function, node.function) + '('), *_expression(node.argument, symbols), _text(')')]
    return []


def readable_apl(source, symbols, *, hero_id=None):
    document = parse(source or '')
    groups, unsupported = OrderedDict(), []
    issue_lines = {issue.range.start.line for issue in document.issues}
    for line in document.lines:
        if isinstance(line, InvalidLine):
            unsupported.append({'line': line.range.start.line, 'text': line.text})
        if not isinstance(line, ActionAssignment):
            continue
        key = line.list_name
        group = groups.setdefault(key, {'name': key, 'label': scenario_title(key, hero_id), 'rows': [], 'entries': [], 'original_targets': []})
        if line.operator == '=':
            group['rows'] = []
            group['original_targets'] = []
        for action in line.actions:
            raw = line.text[action.range.start.column - 1:action.range.end.column - 1]
            if not isinstance(action, ActionEntry) or line.range.start.line in issue_lines:
                group['rows'].append({'unsupported': True, 'raw': raw, 'line': line.range.start.line})
                continue
            options = {opt.name: opt for opt in action.options if isinstance(opt, Option)}
            if action.name in ('call_action_list', 'run_action_list') and 'name' in options:
                group['original_targets'].append(options['name'].value)
            options = filter_options(options, hero_id, symbols)
            if options is None:
                continue
            target = options.get('name') if action.name in ('call_action_list', 'run_action_list') else None
            fields = []
            for opt in options.values():
                if target is opt:
                    continue
                segments = _expression(opt.expression, symbols) if opt.expression else [_text(opt.value)]
                selector = {'min': '取最小值的目标：', 'max': '取最大值的目标：', 'first': '首个符合条件的目标：'}.get(opt.expression_selector, '')
                if selector:
                    segments.insert(0, _text(selector))
                fields.append({'label': OPTION_LABELS.get(opt.name, opt.name), 'segments': segments})
            group['rows'].append({
                **action_guide(action.name, options, symbols),
                'name': action.name, 'label': CONTROL_LABELS.get(action.name, action.name),
                'reference': symbols.get(('action', action.name)), 'fields': fields,
                'target': target.value if target else None,
                'behavior': ('子列表没有执行技能时，继续检查下一项。' if action.name == 'call_action_list' else
                             '转入子列表，停止继续检查当前列表。' if action.name == 'run_action_list' else ''),
                'control': action.name in CONTROL_LABELS, 'raw': raw, 'line': line.range.start.line,
            })
    if hero_id:
        original_targets = {target for group in groups.values() for target in group['original_targets']}
        groups = reachable_groups(groups, original_targets)
    for index, group in enumerate(groups.values()):
        group['anchor'] = f'apl-list-{index}'
    for group in groups.values():
        for index, row in enumerate(group['rows'], start=1):
            row['priority'] = index
            row['anchor'] = f"{group['anchor']}-rule-{index}"
            if row.get('target') is not None:
                target = groups.get(row['target'])
                row['target_anchor'] = target['anchor'] if target else ''
                row['target_label'] = target['label'] if target else row['target']
                if target:
                    target['entries'].append({
                        'parent': group['label'], 'anchor': group['anchor'],
                        'rule_anchor': row['anchor'],
                        'condition': row['condition_guide'], 'text': row['condition_text'],
                        'line': row['line'],
                    })
    return {'groups': list(groups.values()), 'unsupported': unsupported,
            'count': sum(len(g['rows']) for g in groups.values())}
