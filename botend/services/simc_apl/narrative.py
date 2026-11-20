"""将可确认的 APL 判断写成玩家指引；不推测未知变量的战术含义。"""

from decimal import Decimal
import re

from botend.constants.hero_talents import HERO_SUBTREE_NAME_ZH
from botend.constants.wow import SPEC_CN
from .ast import BinaryExpression, CallExpression, IdentifierExpression, NumberExpression, UnaryExpression
from . import condition_terms


RESOURCES = {'rage': '怒气', 'energy': '能量', 'focus': '集中值', 'mana': '法力',
             'runic_power': '符文能量', 'rune': '符文', 'holy_power': '圣能',
             'chi': '真气', 'combo_points': '连击点', 'essence': '精华',
             'soul_shard': '灵魂碎片', 'fury': '恶魔之怒', 'insanity': '狂乱值',
             'astral_power': '星界能量', 'maelstrom': '漩涡值', 'health': '生命值'}
COMPARISONS = {'>': '高于', '>=': '达到', '<': '低于', '<=': '不超过', '=': '为', '!=': '不为'}
INVERSE = {'>': '<=', '>=': '<', '<': '>=', '<=': '>', '=': '!=', '!=': '='}
LIST_WORDS = {'precombat': '战斗准备', 'default': '默认列表', 'cooldowns': '爆发技能',
              'cds': '爆发技能', 'burst': '爆发', 'trinkets': '饰品', 'variables': '条件准备',
              'aoe': '多目标', 'single_target': '单体', 'st': '单体', 'cleave': '顺劈'}


def _slug(value):
    return re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_')


def list_title(name):
    if name is None:
        return '主优先级'
    words = {**{_slug(k): v for k, v in HERO_SUBTREE_NAME_ZH.items()},
             **{_slug(k): v for k, v in SPEC_CN.items()}, **LIST_WORDS}
    parts, remaining = [], name
    while remaining:
        match = next((key for key in sorted(words, key=len, reverse=True)
                      if remaining == key or remaining.startswith(key + '_')), None)
        if not match:
            return name
        parts.append(words[match])
        remaining = remaining[len(match):].lstrip('_')
    return ' · '.join(parts)


def text(value):
    return {'text': value}


def plain(segments):
    return ''.join(s['reference']['name'] if 'reference' in s else
                   '变量「' + s['variable']['name'] + '」' if 'variable' in s else
                   s.get('raw', s.get('text', '')) for s in segments)


def _spell(kind, token, symbols):
    item = symbols.get((kind, token))
    if item is None and kind == 'cooldown':
        item = symbols.get(('action', token))
    if item is None and kind == 'dot':
        item = symbols.get(('action', token))
    if item is None and hasattr(symbols, 'variables'):
        return condition_terms.reference(kind, token, symbols)
    return [{'reference': item}] if item else None


def _predicate(name, symbols, negate=False):
    if name.startswith('target.') and name.split('.')[1] in ('buff', 'debuff', 'cooldown', 'role', 'spec'):
        inner = _predicate(name[7:], symbols, negate)
        return [text('当前目标：'), *inner] if inner is not None else None
    name = condition_terms.contextual_name(name, symbols)
    extra = condition_terms.predicate(name, symbols, negate)
    if extra is not None:
        return extra
    parts = name.split('.')
    if len(parts) == 3 and parts[0] == 'prev_gcd' and parts[1].isdigit():
        ref = _spell('action', parts[2], symbols)
        if ref and int(parts[1]) > 0:
            return [text(f'此前第{parts[1]}个公共冷却技能' + ('不是' if negate else '是')), *ref]
    if len(parts) == 2 and parts[0] in ('hero_tree', 'spec'):
        names = HERO_SUBTREE_NAME_ZH if parts[0] == 'hero_tree' else SPEC_CN
        label = next((v for k, v in names.items() if _slug(k).replace('_', '') == parts[1].replace('_', '')), None)
        if label:
            return [text(('未选择' if negate else '选择了') + label + ('英雄天赋' if parts[0] == 'hero_tree' else '专精'))]
    if len(parts) == 2 and parts[0] == 'talent':
        parts.append('enabled')
    if len(parts) != 3:
        return None
    kind, token, prop = parts
    ref = _spell(kind, token, symbols)
    if ref is None:
        return None
    if kind == 'talent' and prop == 'enabled':
        return [text('未学习' if negate else '已学习'), *ref]
    if kind in ('buff', 'debuff', 'dot') and prop in ('up', 'down', 'ticking'):
        absent = (prop == 'down') != negate
        prefix = '目标' if kind in ('dot', 'debuff') else ''
        return [text(prefix + ('没有' if absent else '具有')), *ref, text('效果')]
    if kind == 'cooldown' and prop == 'ready':
        return [*ref, text('尚未冷却完毕' if negate else '已冷却完毕')]
    if kind == 'cooldown' and prop == 'remains':
        return [*ref, text('已冷却完毕' if negate else '仍在冷却')]
    if kind in ('dot', 'debuff', 'buff') and prop == 'refreshable':
        return [*ref, text('尚未进入刷新窗口' if negate else '已进入刷新窗口')]
    if kind in ('buff', 'debuff') and prop == 'at_max_stacks':
        return [*ref, text('尚未叠满' if negate else '已叠满')]
    return None


def _quantity(name, symbols):
    """返回量的描述及单位；只有语义明确的量才生成正文。"""
    if name.startswith('target.') and name.split('.')[1] in ('buff', 'debuff', 'cooldown'):
        inner = _quantity(name[7:], symbols)
        return ([text('当前目标的'), *inner[0]], inner[1]) if inner is not None else None
    name = condition_terms.contextual_name(name, symbols)
    extra = condition_terms.quantity(name, symbols)
    if extra is not None:
        return extra
    if name in RESOURCES:
        return [text(RESOURCES[name])], '点'
    values = {
        'target.health.pct': ('目标生命值', '%'),
        'target.time_to_die': ('目标预计存活时间', '秒'),
        'fight_remains': ('战斗预计剩余时间', '秒'), 'time': ('开战时间', '秒'),
        'active_enemies': ('敌人数量', '个'), 'gcd': ('公共冷却时间', '秒'),
        'movement.distance': ('移动距离', '码'),
        'gcd.max': ('最长公共冷却时间', '秒'), 'gcd.remains': ('公共冷却剩余时间', '秒'),
        'soul_fragments': ('当前可用灵魂残片数量', '个'),
        # 原生 sc_demon_hunter.cpp 的表达式使用当前 drain_stacks，非未来消耗预测。
        'void_metamorphosis_base_drain_ps': ('虚空变形当前每秒基础怒气消耗', '点'),
        'void_metamorphosis_drain_ps': ('虚空变形当前每秒实际怒气消耗', '点'),
    }
    if name in values:
        label, unit = values[name]
        return [text(label)], unit
    parts = name.split('.')
    if len(parts) == 2 and parts[0] == 'spell_targets':
        ref = _spell('action', parts[1], symbols)
        if ref:
            return [*ref, text('可命中的目标数')], '个'
    if len(parts) == 2 and parts[0] == 'active_dot':
        ref = _spell('dot', parts[1], symbols) or _spell('action', parts[1], symbols)
        if ref:
            return [text('受'), *ref, text('持续效果影响的目标数')], '个'
    if len(parts) == 2 and parts[0] in RESOURCES:
        attrs = {'deficit': ('距上限还缺少', '点'), 'pct': ('比例', '%'),
                 'max': ('上限', '点'), 'time_to_max': ('回满所需时间', '秒'),
                 'regen': ('每秒恢复量', '点')}
        if parts[1] in attrs:
            label, unit = attrs[parts[1]]
            return [text(RESOURCES[parts[0]] + label)], unit
    if len(parts) == 3:
        kind, token, prop = parts
        ref = _spell(kind, token, symbols)
        attrs = {'remains': ('剩余时间', '秒'), 'duration': ('持续时间', '秒'),
                 'stack': ('层数', '层'), 'stacks': ('层数', '层'),
                 'charges': ('可用充能', '次'), 'charges_fractional': ('充能进度', '次'),
                 'full_recharge_time': ('全部充能恢复时间', '秒'), 'rank': ('天赋等级', '级')}
        if kind in ('buff', 'debuff'):
            attrs.update(max_stack=('层数上限', '层'),
                         react=('已过反应时间的层数', '层'), last_trigger=('上次触发时的战斗时间', '秒'))
        if kind == 'action':
            attrs.update(souls_consumed=('此次可消耗的灵魂残片数', '个'),
                         max_souls_consumed=('单次消耗灵魂残片的上限', '个'),
                         execute_time=('执行耗时', '秒'), cast_time=('施法时间', '秒'),
                         travel_time=('飞行时间', '秒'), last_used=('上次使用时的战斗时间', '秒'))
        if ref and prop in attrs:
            label, unit = attrs[prop]
            if kind == 'cooldown' and prop == 'remains':
                label = '冷却剩余时间'
            return [*ref, text(label)], unit
    return None


def _amount(node, symbols):
    if isinstance(node, IdentifierExpression):
        result = _quantity(node.name, symbols)
        if result:
            return result[0]
        predicate = _predicate(node.name, symbols)
        return [text('（'), *predicate, text('时取1，否则取0）')] if predicate else None
    if isinstance(node, NumberExpression):
        return [text(node.value)]
    if isinstance(node, UnaryExpression) and node.operator in ('+', '-', '@'):
        operand = _amount(node.operand, symbols)
        if operand is not None:
            if node.operator == '+':
                return operand
            return [text('（'), *operand, text('的相反数）' if node.operator == '-' else '的绝对值）')]
    if isinstance(node, CallExpression) and node.function in ('floor', 'ceil'):
        operand = _amount(node.argument, symbols)
        if operand is not None:
            return [text('（'), *operand, text('向下取整）' if node.function == 'floor' else '向上取整）')]
    if isinstance(node, BinaryExpression) and node.operator in ('+', '-', '*', '%', '%%', '<?', '>?'):
        left, right = _amount(node.left, symbols), _amount(node.right, symbols)
        if left is not None and right is not None:
            if node.operator == '*' and isinstance(node.right, NumberExpression):
                return [text('（'), *left, text('的' + node.right.value + '倍）')]
            if node.operator in ('<?', '>?'):
                return [text('（'), *left, text('与'), *right,
                        text('中的较大值）' if node.operator == '<?' else '中的较小值）')]
            return [text('（'), *left, text({'+': '加上', '-': '减去', '*': '乘以', '%': '除以', '%%': '取余于'}[node.operator]), *right, text('）')]
    if ((isinstance(node, BinaryExpression) and node.operator in (*COMPARISONS, '&', '|', '^'))
            or (isinstance(node, UnaryExpression) and node.operator == '!')):
        predicate = condition(node, symbols)
        if predicate is not None:
            return [text('（'), *predicate, text('时取1，否则取0）')]
    return None


def _logical_operator(node, negate=False):
    while True:
        simplified = simplify_boolean_comparison(node)
        if simplified is not node:
            node = simplified
        elif isinstance(node, UnaryExpression) and node.operator == '!':
            negate = not negate
            node = node.operand
        else:
            break
    if isinstance(node, BinaryExpression) and node.operator == '^':
        return '^'
    if isinstance(node, BinaryExpression) and node.operator in ('&', '|'):
        return ('|' if node.operator == '&' else '&') if negate else node.operator
    return None


def simplify_boolean_comparison(node):
    """布尔表达式再与数字比较时，用 0/1 真值表化简，不猜测数值字段类型。"""
    if not isinstance(node, BinaryExpression) or node.operator not in COMPARISONS:
        return node
    boolean, number, op = node.left, node.right, node.operator
    if isinstance(boolean, NumberExpression):
        boolean, number = number, boolean
        op = {'>': '<', '>=': '<=', '<': '>', '<=': '>='}.get(op, op)
    is_boolean = ((isinstance(boolean, UnaryExpression) and boolean.operator == '!') or
                  (isinstance(boolean, BinaryExpression) and boolean.operator in (*COMPARISONS, '&', '|', '^')))
    if not is_boolean or not isinstance(number, NumberExpression):
        return node
    value = Decimal(number.value)
    def compare(left):
        return {'=': left == value, '!=': left != value, '>': left > value,
                '>=': left >= value, '<': left < value, '<=': left <= value}[op]
    on_zero, on_one = compare(0), compare(1)
    if on_zero == on_one:
        return NumberExpression('1' if on_one else '0', node.range)
    return boolean if on_one else UnaryExpression('!', boolean, node.range)


def condition(node, symbols, negate=False):
    """将布尔树转为完整语句，混合且/或保持分组；未知子句不冒充完整说明。"""
    simplified = simplify_boolean_comparison(node)
    if simplified is not node:
        return condition(simplified, symbols, negate)
    if isinstance(node, UnaryExpression) and node.operator == '!':
        return condition(node.operand, symbols, not negate)
    if isinstance(node, IdentifierExpression):
        predicate = _predicate(node.name, symbols, negate)
        if predicate is not None:
            return predicate
        quantity = _quantity(node.name, symbols)
        if quantity:
            return [*quantity[0], text('为0' if negate else '不为0')]
        return None
    if isinstance(node, NumberExpression):
        enabled = bool(Decimal(node.value)) != negate
        return [text('条件始终成立' if enabled else '条件始终不成立')]
    if not isinstance(node, BinaryExpression):
        return None
    op = node.operator
    if op in ('&', '|'):
        if negate:
            op = '|' if op == '&' else '&'
        children = [condition(n, symbols, negate) for n in (node.left, node.right)]
        if any(c is None for c in children):
            return None
        for i, child in enumerate((node.left, node.right)):
            child_op = _logical_operator(child, negate)
            if child_op and child_op != op:
                children[i] = [text('（'), *children[i], text('）')]
        return [*children[0], text('，并且' if op == '&' else '，或者'), *children[1]]
    if op == '^':
        left, right = condition(node.left, symbols), condition(node.right, symbols)
        if left is not None and right is not None:
            return [text('以下两个条件' + ('同时成立或同时不成立：' if negate else '恰好一个成立：')),
                    text('（'), *left, text('）；（'), *right, text('）')]
    if op not in COMPARISONS:
        return None
    left, right = node.left, node.right
    if isinstance(left, NumberExpression) and isinstance(right, IdentifierExpression):
        left, right = right, left
        op = {'>': '<', '>=': '<=', '<': '>', '<=': '>='}.get(op, op)
    if negate:
        op = INVERSE[op]
    if not isinstance(left, IdentifierExpression) or not isinstance(right, NumberExpression):
        lhs, rhs = _amount(left, symbols), _amount(right, symbols)
        return [*lhs, text(COMPARISONS[op]), *rhs] if lhs is not None and rhs is not None else None
    number = Decimal(right.value)
    boolean_property = left.name.split('.')[-1] in ('enabled', 'up', 'down', 'ticking', 'ready', 'refreshable', 'at_max_stacks')
    boolean_identity = len(left.name.split('.')) == 2 and left.name.split('.')[0] in ('talent', 'hero_tree', 'spec')
    boolean_history = left.name.startswith('prev_gcd.')
    if (boolean_property or boolean_identity or boolean_history) and number in (0, 1) and op in ('=', '!='):
        predicate = _predicate(left.name, symbols, (number == 0) != (op == '!='))
        if predicate:
            return predicate
    quantity = _quantity(left.name, symbols)
    if quantity is None:
        # 布尔字段也可以显式比较 0/1；只对真正的布尔量采用此简写。
        if number in (0, 1) and op in ('=', '!='):
            return _predicate(left.name, symbols, (number == 0) != (op == '!='))
        return None
    parts, unit = quantity
    if left.name == 'active_enemies' and op in ('>=', '=', '<=', '>', '<'):
        prefix = {'>=': '至少有', '=': '有且仅有', '<=': '最多有', '>': '多于', '<': '少于'}[op]
        return [text(f'{prefix}{right.value}个敌人')]
    if left.name.startswith('cooldown.') and left.name.endswith('.remains') and number == 0 and op in ('=', '<='):
        return [*_spell('cooldown', left.name.split('.')[1], symbols), text('已冷却完毕')]
    return [*parts, text(COMPARISONS[op] + right.value + unit)]


def action_guide(action, options, symbols):
    """主行只呈现使用时机，执行参数留在展开内容，不擅自补写战术理由。"""
    check = options.get('if')
    from .condition_rendering import describe_condition, describe_amount
    predicate = describe_condition(check.expression, symbols) if check and check.expression else None
    decision_guide = predicate and predicate[0].get('text') == '按情况判断：①'
    if check:
        guide = ([*predicate, text('。满足对应条件后')] if decision_guide else
                 [text('当'), *predicate, text('时')]) if predicate else [text('满足此项的额外判定后')]
    else:
        guide = []
    endings = {'call_action_list': '检查这组优先级', 'run_action_list': '转入这组优先级',
               'variable': '更新后续判断所需的变量', 'cycling_variable': '逐个目标更新判断变量',
               'pool_resource': '等待资源', 'wait': '等待后再行动',
               'snapshot_stats': '记录开战时的属性', 'auto_attack': '开启自动攻击'}
    ending = endings.get(action, '使用')
    if not check and action not in endings:
        ending = '前面的技能均未执行时，作为下一选择'
    guide.append(text(ending + '。'))
    selector = options.get('target_if')
    target = None
    if selector and selector.expression_selector in ('min', 'max') and isinstance(selector.expression, IdentifierExpression):
        quantity = _quantity(selector.expression.name, symbols)
        if quantity:
            target = [text('先选择'), *quantity[0], text('最低' if selector.expression_selector == 'min' else '最高'), text('的目标；再检查本项的使用条件。')]
    if selector and target is None and selector.expression:
        selection = {'min': '选择以下数值最小的目标：', 'max': '选择以下数值最大的目标：',
                     'first': '选择首个满足以下条件的目标：'}.get(selector.expression_selector, '目标筛选：')
        rendered = (describe_condition(selector.expression, symbols) if selector.expression_selector == 'first'
                    else describe_amount(selector.expression, symbols))
        target = [text(selection), *rendered, text('；再检查本项的使用条件。')]
    if selector and target is None:
        target = [text('此项另有目标筛选规则，见详细判定。')]
    extra = set(options) - {'if', 'name'} - ({'target_if'} if target else set())
    if extra or selector:
        guide.append(text('另有目标或执行设置。'))
    return {'guide': guide, 'guide_text': plain(guide), 'target_guide': target,
            'condition_guide': predicate, 'condition_text': plain(predicate) if predicate else
            ('满足额外判定（展开原始规则查看）' if check else '无额外进入条件')}
