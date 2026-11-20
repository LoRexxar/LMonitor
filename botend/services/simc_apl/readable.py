"""将 APL 解析为玩家可阅读的优先级，保留条件、列表重置与未知语法。"""

from collections import OrderedDict, defaultdict

from botend.constants.wow import canonical_class_spec
from botend.models import SimcAplSymbolScope, WowTalentNodeMetadata
from botend.services.simc_apl.ast import (
    ActionAssignment, ActionEntry, InvalidLine, Option,
)
from botend.services.simc_apl.catalog import query_visible_symbols
from botend.services.simc_apl.translation import CONTROL_ACTIONS
from botend.services.simc_apl.parser import parse
from botend.services.simc_apl.narrative import action_guide
from botend.services.simc_apl.hero_filter import filter_options, reachable_groups, scenario_title
from botend.services.simc_apl.routing import annotate_routes
from botend.services.simc_apl.condition_context import ConditionContext
from botend.services.simc_apl.condition_rendering import describe_condition, describe_amount, diagnostics
from botend.services.simc_apl.condition_variables import collect_variables, attach_variable_details
from botend.services.simc_apl.hierarchy import label_groups, build_hierarchy
from botend.services.wow_spell_display import SpellDisplayReader, plain_text
from botend.templatetags.wow_tags import wow_icon_oss_url


CONTROL_LABELS = {
    'call_action_list': '检查子优先级', 'run_action_list': '转入子优先级',
    'variable': '设置变量', 'cycling_variable': '按目标设置变量',
    'pool_resource': '等待资源', 'wait': '等待', 'auto_attack': '开始自动攻击',
    'snapshot_stats': '记录战斗开始属性', 'potion': '使用药水',
    'use_item': '使用装备', 'use_items': '使用可用装备',
    'invoke_external_buff': '请求外部增益', 'cancel_buff': '取消增益',
    'retarget_auto_attack': '切换自动攻击目标',
    'retarget': '重新选择目标', 'cancel_action': '中断当前动作',
    'strict_sequence': '依次执行技能序列', 'pick_up_fragment': '拾取灵魂残片',
    'apply_poison': '涂抹毒药', 'summon_pet': '召唤宠物',
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


def _talent_spell_id(entry):
    # DB2 backfill stores EntryID in spell_id when a definition has no spell.
    # Only an explicit display relation can validate that same numeric value.
    if entry.display_spell_id and entry.display_spell_id > 0:
        return entry.display_spell_id
    if entry.spell_id and entry.spell_id > 0 and entry.spell_id != entry.node_id:
        return entry.spell_id
    return None


def _talent_entry(entries, runtime_spell_id, definition_spells=None):
    if runtime_spell_id:
        entries = [entry for entry in entries if runtime_spell_id == _talent_spell_id(entry)
                   or (definition_spells or {}).get(entry.node_id) == runtime_spell_id or (
            entry.spell_id == runtime_spell_id and entry.spell_id != entry.node_id
        )]
    # Even a shared display spell does not prove which Entry override to use.
    # Never resolve choice/multi-entry nodes by query order or localized name.
    return entries[0] if len(entries) == 1 else None


def display_symbols(class_name, spec_name, talent_version=None, *, branch='retail'):
    """仅按已验证的 token 类型和 ID 关联提示，不用名字猜测技能或天赋。"""
    hero_trees = list(SimcAplSymbolScope.objects.filter(
        class_name=class_name, spec=spec_name, is_active=True, symbol__is_active=True,
    ).exclude(hero_tree__isnull=True).exclude(hero_tree='').values_list('hero_tree', flat=True).distinct())
    candidates = {}
    for hero in [None, *hero_trees]:
        for row in query_visible_symbols(class_name, spec_name, hero):
            # Engine operations have labels and semantics, not a game-spell tooltip.
            if row.symbol_kind == 'action' and row.token in CONTROL_ACTIONS:
                continue
            candidates.setdefault((row.symbol_kind, row.token), {})[row.pk] = row
    symbols = {}
    for key, rows in candidates.items():
        identities = {(r.spell_id, r.trait_id) for r in rows.values()}
        if len(identities) == 1:
            symbols[key] = next(iter(rows.values()))
    trait_ids = {r.trait_id for r in symbols.values() if r.symbol_kind == 'talent' and r.trait_id}
    entries_by_trait = defaultdict(list)
    if talent_version and trait_ids:
        canonical_class, canonical_spec = canonical_class_spec(class_name, spec_name) or (class_name, spec_name)
        rows = WowTalentNodeMetadata.objects.filter(
            talent_version=talent_version, class_name__iexact=canonical_class,
            spec_name__iexact=canonical_spec, talent_id__in=trait_ids,
        )
        for row in rows:
            # Runtime trait_id is TraitNode; metadata node_id is TraitNodeEntry.
            entries_by_trait[row.talent_id].append(row)
    traits = {
        key: _talent_entry(entries_by_trait[row.trait_id], row.spell_id)
        for key, row in symbols.items() if row.symbol_kind == 'talent'
    }
    unresolved = {key: row for key, row in symbols.items()
                  if row.symbol_kind == 'talent' and row.spell_id and traits[key] is None}
    if unresolved:
        from .talent_entry_source import definition_spell_ids
        source_spells = definition_spell_ids(talent_version, {
            entry.node_id for row in unresolved.values() for entry in entries_by_trait[row.trait_id]
        })
        for key, row in unresolved.items():
            traits[key] = _talent_entry(entries_by_trait[row.trait_id], row.spell_id, source_spells)
    reader = SpellDisplayReader(talent_version.branch if talent_version else branch)
    reader.preload({r.spell_id for r in symbols.values() if r.symbol_kind != 'talent' and r.spell_id} | {
        _talent_spell_id(trait) for trait in traits.values() if trait
    })
    result = {}
    for key, row in symbols.items():
        trait = traits.get(key)
        # TraitNodeEntry ID 与技能 ID 不是同一命名空间。
        sid = _talent_spell_id(trait) if trait else (row.spell_id if row.symbol_kind != 'talent' else None)
        data = reader.get(sid, class_name=class_name, spec_name=spec_name,
                          node_id=trait.node_id if trait else None) if sid else {}
        name = data.get('name_zh') or (trait.name_zh if trait else '') or row.name_zh or data.get('name') or row.name_en or row.token
        description = data.get('description_zh') or data.get('description') or (
            plain_text(trait.description_zh) or plain_text(trait.description) if trait else '')
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
            group['rows'].append({
                'name': action.name, 'label': CONTROL_LABELS.get(action.name, action.name),
                'reference': None if action.name in CONTROL_ACTIONS else symbols.get(('action', action.name)),
                '_display_options': options,
                **({'_routing_options': options} if target else {}),
                'target': target.value if target else None,
                'behavior': ('子列表没有执行技能时，继续检查下一项。' if action.name == 'call_action_list' else
                             '转入子列表，停止继续检查当前列表。' if action.name == 'run_action_list' else ''),
                'control': action.name in CONTROL_ACTIONS, 'raw': raw, 'line': line.range.start.line,
            })
    if hero_id:
        original_targets = {target for group in groups.values() for target in group['original_targets']}
        groups = reachable_groups(groups, original_targets)
    label_groups(groups, symbols)
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
    variables = collect_variables(groups)
    context = ConditionContext(symbols, variables=variables)
    for group in groups.values():
        for row in group['rows']:
            if row.get('unsupported'):
                continue
            local = context.for_action(row['name'])
            options = row['_display_options']
            fields = []
            for opt in options.values():
                if row['target'] is not None and opt.name == 'name':
                    continue
                if opt.expression:
                    boolean = opt.name in ('if', 'condition', 'interrupt_if', 'cancel_if', 'early_chain_if') or opt.expression_selector == 'first'
                    segments = (describe_condition if boolean else describe_amount)(opt.expression, local)
                else:
                    segments = [_text(opt.value)]
                selector = {'min': '取最小值的目标：', 'max': '取最大值的目标：', 'first': '首个符合条件的目标：'}.get(opt.expression_selector, '')
                if selector:
                    segments.insert(0, _text(selector))
                fields.append({'name': opt.name, 'label': OPTION_LABELS.get(opt.name, opt.name), 'segments': segments})
            row.update(action_guide(row['name'], options, local), fields=fields)
            row['condition_diagnostics'] = diagnostics([p for field in fields for p in field['segments']])
            row['has_unexplained_conditions'] = any(d['reason'] in ('unknown_semantics', 'undefined_variable') for d in row['condition_diagnostics'])
    attach_variable_details(groups, variables)
    annotate_routes(groups, context)
    hierarchy = build_hierarchy(groups)
    return {'groups': list(groups.values()), 'hierarchy': hierarchy, 'unsupported': unsupported,
            'count': sum(len(g['rows']) for g in groups.values())}
