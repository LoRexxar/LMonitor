"""按引擎对象和字段类型解释条件；不从字段拼写猜测战术意图。

通用定义对应 engine/action/{action,dot}.cpp、engine/sim/{cooldown,raid_event}.cpp、
engine/player/{player,unique_gear}.cpp。专精表达式另按原生实现维护。
"""

import re
from .condition_class_terms import VALUES as CLASS_VALUES, PREDICATES as CLASS_PREDICATES


def text(value):
    return {'text': value}


STATS = {'strength': '力量', 'agility': '敏捷', 'intellect': '智力', 'stamina': '耐力',
         'mastery': '精通', 'haste': '急速', 'crit': '暴击', 'versatility': '全能',
         'attack_power': '攻击强度', 'spell_power': '法术强度', 'any_dps': '输出属性',
         'all': '全属性'}
COOLDOWN_VALUES = {
    'remains': ('冷却剩余时间', '秒'), 'duration': ('冷却时长', '秒'),
    'base_duration': ('基础冷却时长', '秒'), 'remains_expected': ('预计冷却剩余时间', '秒'),
    'remains_guess': ('预计冷却剩余时间', '秒'), 'charges': ('可用充能', '次'),
    'charges_fractional': ('充能进度', '次'), 'max_charges': ('充能上限', '次'),
    'recharge_time': ('恢复下一次充能所需时间', '秒'),
    'full_recharge_time': ('全部充能恢复时间', '秒'),
}
DOT_VALUES = {
    'remains': ('持续效果剩余时间', '秒'), 'duration': ('持续效果完整时长', '秒'),
    'ticks': ('持续效果已跳次数', '次'), 'ticks_remain': ('持续效果剩余跳数', '次'),
    'ticks_remain_fractional': ('持续效果剩余跳数（含小数）', '次'),
    'tick_time_remains': ('距持续效果下一跳的时间', '秒'),
    'pmultiplier': ('持续效果记录的持续倍率', ''), 'active_dots': ('持续效果覆盖的目标数', '个'),
    'max_stacks': ('持续效果层数上限', '层'),
}
ACTION_VALUES = {
    **{k: v for k, v in COOLDOWN_VALUES.items() if k not in ('remains', 'duration', 'base_duration', 'remains_expected', 'remains_guess')},
    **DOT_VALUES,
    'cost': ('本次资源消耗', '点'), 'base_cost': ('基础资源消耗', '点'),
    'cast_time': ('施法时间', '秒'), 'execute_time': ('执行耗时', '秒'),
    'execute_remains': ('当前施放剩余时间', '秒'), 'cast_remains': ('当前施法剩余时间', '秒'),
    'channel_remains': ('当前引导剩余时间', '秒'), 'travel_time': ('飞行时间', '秒'),
    'in_flight_remains': ('飞行中的效果最早命中所需时间', '秒'),
    'in_flight_count': ('飞行中的效果数量', '个'),
    'in_flight_to_target_count': ('飞向当前目标的效果数量', '个'),
    'cooldown': ('冷却时长', '秒'), 'usable_in': ('冷却及动作锁定结束前的等待时间', '秒'),
    'energize_amount': ('本次产生的资源', '点'), 'persistent_multiplier': ('本次施放的持续倍率', ''),
    'damage': ('预期直接伤害', ''), 'hit_damage': ('普通命中直接伤害', ''),
    'crit_damage': ('暴击直接伤害', ''), 'tick_time': ('当前持续效果每跳间隔', '秒'),
    'new_tick_time': ('新施放持续效果每跳间隔', '秒'), 'active_dots': ('持续效果覆盖的目标数', '个'),
}
CURRENT_DOT = frozenset(DOT_VALUES) | {'refreshable', 'ticking', 'pmultiplier'}
CURRENT_ACTION = frozenset(ACTION_VALUES) | {
    'ready', 'usable', 'cooldown_react', 'cost_affordable', 'executing', 'casting', 'channeling',
    'in_flight', 'in_flight_to_target', 'spell_targets', 'demonsurge_available',
}


def contextual_name(name, symbols):
    """隐式字段由所在动作决定，不能解释成角色的全局状态。"""
    action = getattr(symbols, 'action', '')
    if not action:
        return name
    if name == 'spell_targets':
        return f'spell_targets.{action}'
    if name in CURRENT_DOT:
        return f'dot.{action}.{name}'
    if name in CURRENT_ACTION:
        return f'action.{action}.{name}'
    return name


def reference(kind, token, symbols):
    item = symbols.get((kind, token))
    if item is None and kind in ('cooldown', 'dot'):
        item = symbols.get(('action', token))
    if item is not None:
        return [{'reference': item}]
    # 保留原身份，不把缺少中文或提示数据当成整个属性无法说明。
    return [{'raw': f'{kind}.{token}', 'reason': 'missing_identity'}]


def _trinket(parts):
    if parts[0] in ('this_trinket', 'other_trinket'):
        return [text('当前使用的饰品' if parts[0] == 'this_trinket' else '另一件饰品')], parts[1:]
    if parts[0] == 'trinket' and len(parts) > 2:
        owner = [text(f'饰品{parts[1]}')] if parts[1] in ('1', '2') else [text('饰品「'),
            {'raw': parts[1], 'reason': 'item_identity'}, text('」')]
        return owner, parts[2:]
    return None


def quantity(name, symbols):
    parts = name.split('.')
    if len(parts) == 2 and parts[0] == 'variable':
        definition = getattr(symbols, 'variables', {}).get(parts[1])
        if definition:
            return [{'variable': {k: definition[k] for k in ('name', 'anchor', 'rule_anchor')}}, text('的当前值')], ''
        return None
    if len(parts) == 3 and parts[0] in ('cooldown', 'dot', 'action'):
        kind, token, prop = parts
        table = {'cooldown': COOLDOWN_VALUES, 'dot': DOT_VALUES, 'action': ACTION_VALUES}[kind]
        if prop in table:
            label, unit = table[prop]
            return [*reference(kind, token, symbols), text(label)], unit
    if len(parts) == 3 and parts[0] in ('buff', 'debuff') and parts[2] in ('value', 'default_value', 'stack_value'):
        label = {'value': '当前效果数值', 'default_value': '默认效果数值', 'stack_value': '叠层后的效果数值'}[parts[2]]
        return [*reference(parts[0], parts[1], symbols), text(label)], ''
    trinket = _trinket(parts)
    if trinket:
        owner, attrs = trinket
        if len(attrs) == 2 and attrs[0] == 'cooldown' and attrs[1] in COOLDOWN_VALUES:
            label, unit = COOLDOWN_VALUES[attrs[1]]
            return [*owner, text(label)], unit
        if len(attrs) == 1 and attrs[0] in ('ilvl', 'cast_time'):
            label, unit = ('物品等级', '') if attrs[0] == 'ilvl' else ('使用施法时间', '秒')
            return [*owner, text(label)], unit
        if len(attrs) == 3 and attrs[0] in ('proc', 'buff', 'stat') and attrs[1] in STATS:
            label = {'duration': ('增益完整时长', '秒'), 'remains': ('增益剩余时间', '秒'),
                     'default_value': ('增益默认数值', ''), 'value': ('增益当前数值', ''), 'stack': ('增益层数', '层')}.get(attrs[2])
            if label:
                return [*owner, text(f'的{STATS[attrs[1]]}{label[0]}（多个效果取最大值）')], label[1]
    if len(parts) == 3 and parts[0] == 'raid_event':
        event = {'adds': '增援敌人', 'movement': '移动', 'pull': '下一波交战'}.get(parts[1])
        label = {'in': ('距离下次事件开始', '秒'), 'remains': ('当前事件剩余时间', '秒'),
                 'count': ('事件的目标数量', '个'), 'duration': ('事件时长', '秒'),
                 'distance': ('事件移动距离', '码'), 'cooldown': ('事件间隔', '秒')}.get(parts[2])
        if event and label:
            return [text(event + label[0])], label[1]
    values = {'time_to_die': ('当前目标预计存活时间', '秒'),
              'time_to_die.remains': ('当前目标预计存活时间', '秒'),
              'target.time_to_die.remains': ('当前目标预计存活时间', '秒'),
              'target.distance': ('与当前目标的距离', '码'), 'target.health': ('当前目标生命值', '点'),
              'desired_targets': ('场景设定的目标数量', '个'),
              'expected_combat_length': ('预计战斗总时长', '秒'),
              'spell_haste': ('法术施法时间缩放系数', ''), 'attack_haste': ('攻击时间缩放系数', ''),
              'soul_fragments.total': ('全部灵魂残片数量（含尚未激活）', '个'),
              'soul_fragments.inactive': ('尚未激活的灵魂残片数量', '个')}
    values.update(CLASS_VALUES)
    if name in values:
        label, unit = values[name]
        return [text(label)], unit
    if len(parts) == 2 and parts[0] == 'stat' and parts[1].removesuffix('_rating') in STATS:
        return [text(STATS[parts[1].removesuffix('_rating')] + ('等级' if parts[1].endswith('_rating') else '数值'))], ''
    if len(parts) == 2 and parts[0] in ('active_dots', 'dot_refreshable_count'):
        return [*reference('dot', parts[1], symbols), text('覆盖的目标数' if parts[0] == 'active_dots' else '可刷新的目标数')], '个'
    return None


def predicate(name, symbols, negate=False):
    parts = name.split('.')
    if len(parts) == 3 and parts[0] == 'cooldown' and parts[2] in ('up', 'ready'):
        return [*reference('cooldown', parts[1], symbols), text('尚未冷却完毕' if negate else '已冷却完毕')]
    if len(parts) == 3 and parts[0] == 'action':
        labels = {'ready': ('当前满足使用检查', '当前不满足使用检查'),
                  'usable': ('冷却计时已就绪', '冷却计时尚未就绪'),
                  'cooldown_react': ('已冷却完毕且已过重置反应时间', '尚未冷却完毕或仍在重置反应时间内'),
                  'cost_affordable': ('当前资源足以支付消耗', '当前资源不足以支付消耗'),
                  'executing': ('正在施法或引导', '未在施法或引导'),
                  'casting': ('正在施法', '未在施法'), 'channeling': ('正在引导', '未在引导'),
                  'in_flight': ('有尚未命中的效果', '没有尚未命中的效果'),
                  'in_flight_to_target': ('有飞向当前目标的效果', '没有飞向当前目标的效果'),
                  'demonsurge_available': ('可触发恶魔涌动', '不可触发恶魔涌动'),
                  'placed': ('符咒已放置且尚未激活', '没有等待激活的符咒'),
                  'enabled': ('已启用', '未启用')}
        if parts[2] in labels:
            return [*reference('action', parts[1], symbols), text(labels[parts[2]][negate])]
    if len(parts) == 2 and parts[0] in ('prev', 'prev_off_gcd'):
        prefix = ('最近一个主动技能' + ('不是' if negate else '是')) if parts[0] == 'prev' else (
            '上次公共冷却技能之后' + ('未使用过' if negate else '使用过'))
        return [text(prefix), *reference('action', parts[1], symbols)]
    if len(parts) == 2 and parts[0] == 'apex' and parts[1].isdigit():
        return [text(('未学习' if negate else '已学习') + f'巅峰天赋第{parts[1]}个节点')]
    if len(parts) == 2 and parts[0] == 'fight_style':
        label = {'patchwerk': '木桩战', 'castingpatchwerk': '施法木桩战', 'dungeonroute': '地下城路线',
                 'dungeonslice': '地下城片段', 'hecticaddcleave': '频繁增援顺劈战'}.get(parts[1])
        if label:
            return [text('模拟场景' + ('不是' if negate else '为') + label)]
    if len(parts) == 2 and parts[0] == 'role':
        role = {'attack': '攻击型输出', 'spell': '施法型输出', 'dps': '输出', 'tank': '坦克', 'heal': '治疗'}.get(parts[1])
        if role:
            return [text('模拟职责' + ('不是' if negate else '为') + role)]
    if len(parts) == 3 and parts[0] == 'raid_event':
        event = {'adds': '增援敌人', 'movement': '移动', 'pull': '交战波次'}.get(parts[1])
        labels = {'exists': ('场景包含', '场景不包含'), 'up': ('当前处于', '当前未处于')}
        if event and parts[2] in labels:
            return [text(labels[parts[2]][negate] + event + '事件')]
        if event and parts[2] == 'has_boss':
            return [text(event + ('不包含首领' if negate else '包含首领'))]
    if len(parts) == 2 and parts[0] == 'equipped':
        return [text('未装备' if negate else '已装备'), {'raw': parts[1], 'reason': 'item_identity'}]
    if len(parts) == 2 and parts[0] == 'consumable' and getattr(symbols, 'action', '') in ('potion', 'flask', 'food', 'augmentation'):
        return [text('当前动作配置的消耗品' + ('不是' if negate else '是')),
                {'raw': parts[1], 'reason': 'item_identity'}, text('（忽略品质后缀）')]
    if len(parts) == 2 and parts[0] in ('main_hand', 'off_hand') and parts[1] in ('1h', '2h'):
        return [text(('主手' if parts[0] == 'main_hand' else '副手') + ('不是' if negate else '是') +
                     ('双手武器' if parts[1] == '2h' else '单手武器（含小型武器）'))]
    trinket = _trinket(parts)
    if trinket:
        owner, attrs = trinket
        if attrs == ['is']:
            return None
        if len(attrs) == 2 and attrs[0] == 'is':
            return [*owner, text('不是' if negate else '是'), {'raw': attrs[1], 'reason': 'item_identity'}]
        labels = {'has_use_buff': '主动使用的输出属性增益', 'has_use_damage': '主动使用的伤害效果',
                  'has_cooldown': '带冷却的效果', 'has_use': '主动使用效果'}
        if len(attrs) == 1 and attrs[0] in labels:
            return [*owner, text(('不具有' if negate else '具有') + labels[attrs[0]])]
        if len(attrs) == 2 and attrs[0] in ('has_buff', 'has_stat') and attrs[1] in STATS:
            return [*owner, text(('不具有' if negate else '具有') + STATS[attrs[1]] + '增益效果')]
        if len(attrs) == 2 and attrs[0] == 'cooldown' and attrs[1] in ('ready', 'up'):
            return [*owner, text('尚未冷却完毕' if negate else '已冷却完毕')]
        if len(attrs) == 3 and attrs[0] in ('proc', 'stat', 'buff') and attrs[1] in STATS and attrs[2] in ('up', 'down'):
            absent = (attrs[2] == 'down') != negate
            return [*owner, text('的' + STATS[attrs[1]] + '增益' + ('未生效' if absent else '正在生效'))]
    if len(parts) == 3 and parts[0] == 'pet' and parts[2] == 'active':
        item = symbols.get(('pet', parts[1]))
        owner = [{'reference': item}] if item else [text('召唤物「'), {'raw': parts[1], 'reason': 'pet_identity'}, text('」')]
        return [*owner, text('尚未激活' if negate else '正在场上')]
    if len(parts) == 2 and parts[0] == 'set_bonus':
        match = re.fullmatch(r'([a-z]+\d+)_(\d+)pc', parts[1])
        if match:
            return [text(('未激活' if negate else '已激活') + match[1].upper() + '套装' + match[2] + '件效果')]
    labels = {'in_combat': ('正在战斗中', '尚未进入战斗'),
              'in_boss_encounter': ('当前是首领战', '当前不是首领战'),
              'target.is_boss': ('当前目标是首领', '当前目标不是首领'),
              'target.has_absorb': ('当前目标有吸收护盾', '当前目标没有吸收护盾')}
    labels.update(CLASS_PREDICATES)
    if name in labels:
        return [text(labels[name][negate])]
    return None
