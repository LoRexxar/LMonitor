"""用原版 SimC 的装备导出生成并核验同属性、无自带特效的装备对照。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from functools import lru_cache

MARKER = '# lmonitor_equipment_control_v1='
NATIVE_SLOTS = frozenset(('head', 'neck', 'shoulders', 'back', 'chest', 'wrists',
                   'hands', 'waist', 'legs', 'feet', 'finger1', 'finger2',
                   'main_hand', 'off_hand'))
ALIASES = {'shoulder': 'shoulders', 'wrist': 'wrists', 'hand': 'hands',
           'ring1': 'finger1', 'ring2': 'finger2'}
# 应用快照与原生 SimC 的槽位拼写不同，入口统一接受这些别名。
SLOTS = NATIVE_SLOTS | frozenset(ALIASES)
ALL_SLOTS = NATIVE_SLOTS | {'trinket1', 'trinket2'}
PENDING = 'lmonitor_effect_control_pending,stats=lmonitor_unprepared'


def candidate_swaps(params):
    """保持旧单件输入兼容，组合以完整槽位列表表示。"""
    if isinstance(params.get('gear_swaps'), list):
        return params['gear_swaps']
    swap = params.get('gear_swap')
    return [swap] if isinstance(swap, dict) else []


@lru_cache(maxsize=1)
def equipment_rules():
    return json.loads(Path(__file__).with_name('simc_equipment_rules.json').read_text(encoding='utf-8'))


def validate_effect_policy(params):
    """只接受冻结的背景规则和与候选完全一致的目标槽位。"""
    policy = params.get('equipment_effect_policy')
    slots = [ALIASES.get(swap.get('slot'), swap.get('slot')) for swap in candidate_swaps(params)]
    if (params.get('candidate_type') != 'gear_swap' or not isinstance(policy, dict)
            or set(policy) != {'version', 'target_slots', 'rules'} or policy['version'] != 2
            or not slots or len(set(slots)) != len(slots) or any(slot not in SLOTS for slot in slots)
            or policy['target_slots'] != slots):
        raise ValueError('装备特效背景策略无效')
    rules = policy['rules']
    if (not isinstance(rules, dict) or set(rules) != {'version', 'embellishments', 'effect_ids', 'intrinsic_item_ids', 'sets'}
            or rules['version'] != 2 or not isinstance(rules['embellishments'], dict)
            or any(not isinstance(rules[key], list) for key in ('effect_ids', 'intrinsic_item_ids', 'sets'))):
        raise ValueError('装备特效规则无效')
    for key in ('effect_ids', 'intrinsic_item_ids'):
        if any(type(value) is not int or value <= 0 for value in rules[key]):
            raise ValueError('装备特效身份无效')
    for name, value in rules['embellishments'].items():
        if (not re.fullmatch(r'[a-z0-9_]+', name) or not isinstance(value, dict)
                or set(value) != {'bonus_id', 'spell_id'}
                or any(type(number) is not int or number <= 0 for number in value.values())):
            raise ValueError('美化规则无效')
    for row in rules['sets']:
        if (not isinstance(row, dict) or set(row) != {'name', 'pieces', 'items'}
                or not isinstance(row['name'], str) or not re.fullmatch(r'[a-z0-9_]+', row['name'])
                or type(row['pieces']) is not int or not 1 <= row['pieces'] <= 8
                or not isinstance(row['items'], list)
                or any(type(number) is not int or number <= 0 for number in row['items'])):
            raise ValueError('两件套规则无效')


def mark_equipment_input(code, slots, *, control, rules):
    """冻结多件候选与服务端规则；两个模拟组都准备同一无美化背景。"""
    if (not isinstance(slots, list) or not slots or len(set(slots)) != len(slots)
            or any(slot not in SLOTS for slot in slots) or MARKER in code):
        raise ValueError('装备组合槽位无效')
    lines, originals = code.splitlines(), {}
    for index, line in enumerate(lines):
        key, sep, value = line.strip().partition('=')
        slot = ALIASES.get(key, key)
        if sep and slot in slots:
            if slot in originals:
                raise ValueError('装备组合包含重复槽位')
            originals[slot] = value
            lines[index] = f'{slot}={PENDING}'
    if set(originals) != set(slots):
        raise ValueError('装备组合缺少目标装备槽')
    payload = {'version': 2, 'items': originals, 'control': control, 'rules': rules}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return '\n'.join(lines) + '\n' + MARKER + encoded + '\n'


def control_key(candidate_key):
    return 'effect-control-' + hashlib.sha256(candidate_key.encode()).hexdigest()[:32]


def mark_control_input(code, slot):
    """冻结原装备，使用无法被旧工作器误执行的占位装备。"""
    slot = ALIASES.get(slot, slot)
    if slot not in NATIVE_SLOTS or MARKER in code:
        raise ValueError('装备特效对照槽位或输入无效')
    lines, matched = code.splitlines(), []
    for index, line in enumerate(lines):
        key, sep, value = line.strip().partition('=')
        if sep and ALIASES.get(key, key) == slot:
            matched.append((index, key, value))
    if len(matched) != 1:
        raise ValueError('装备特效对照必须恰好包含一个目标装备槽')
    index, key, value = matched[0]
    payload = base64.urlsafe_b64encode(json.dumps(
        {'slot': slot, 'value': value}, ensure_ascii=False,
    ).encode()).decode()
    lines[index] = f'{key}={PENDING}'
    return '\n'.join(lines) + '\n' + MARKER + payload + '\n'


def _options(value):
    return dict(part.split('=', 1) for part in value.split(',') if '=' in part)


def _stats(value):
    result = {}
    for token in value.split(','):
        token = token.strip()
        if not token:
            continue
        match = re.fullmatch(r'\+?(-?\d+(?:\.\d+)?)\s+([A-Za-z]+)', token)
        if not match:
            raise ValueError(f'SimC 装备属性格式无法识别：{token}')
        amount, stat = match.groups()
        stat = stat.lower()
        result[stat] = result.get(stat, 0) + float(amount)
    return result


def parse_equipment_export(profile, log, slot):
    """读取原生保存的装备字段及初始化日志中的实际护甲、武器数值。"""
    slot = ALIASES.get(slot, slot)
    lines = profile.splitlines()
    rows = [i for i, line in enumerate(lines)
            if ALIASES.get(line.partition('=')[0], line.partition('=')[0]) == slot]
    if len(rows) != 1:
        raise ValueError('SimC 装备导出缺少唯一目标槽位')
    index = rows[0]
    options = _options(lines[index].partition('=')[2])
    if index + 1 < len(lines) and lines[index + 1].startswith('# '):
        options = {**_options(lines[index + 1][2:]), **options}
    records = [line for line in log.splitlines()
               if re.search(rf'\bslot={re.escape(slot)}\s', line)
               and ' name=' in line and ' source=' in line]
    if len(records) != 1:
        raise ValueError('SimC 初始化日志缺少唯一装备记录，拒绝猜测属性')
    record = records[0]
    blocks = dict(re.findall(r'(\w+)=\{\s*([^{}]*?)\s*\}', record))
    if 'stats' not in blocks and slot not in ('trinket1', 'trinket2'):
        raise ValueError('SimC 未导出装备基础属性')
    stats = _stats(blocks.get('stats', ''))
    for stat, value in _stats(blocks.get('socket_bonus', '')).items():
        stats[stat] = stats.get(stat, 0) + value
    weapon = None
    if 'damage' in blocks:
        damage = re.fullmatch(r'([\d.]+)\s*-\s*([\d.]+)', blocks['damage'])
        speed = re.search(r'\bspeed=([\d.]+)', record)
        if not damage or not speed or not options.get('weapon'):
            raise ValueError('SimC 未完整导出武器类型、伤害与攻速')
        weapon = (options['weapon'].split('_')[0], *damage.groups(), speed[1])
    return {'options': options, 'stats': stats, 'weapon': weapon,
            'attachments': {key: blocks.get(key, '') for key in
                            ('gems', 'enchant', 'addon', 'temporary_enchant')},
            'record': record}


def synthetic_item(export):
    """仅复制静态属性和独立宝石附魔；不复制 ID、奖励或装备自身效果。"""
    options = export['options']
    values = ['lmonitor_effect_control']
    for key in ('ilevel', 'quality', 'type'):
        if options.get(key):
            values.append(f'{key}={options[key]}')
    values.append('stats=' + '_'.join(
        f'{amount:g}{stat}' for stat, amount in sorted(export['stats'].items())
    ))
    if export['weapon']:
        kind, minimum, maximum, speed = export['weapon']
        values.append(f'weapon={kind}_{speed}speed_{minimum}min_{maximum}max')
    # 使用宝石身份保留特殊宝石效果；没有身份时才使用原生导出的数值。
    if options.get('gem_id'):
        for key in ('gem_id', 'gem_bonus_id', 'gem_ilevel'):
            if options.get(key):
                values.append(f'{key}={options[key]}')
    elif options.get('gems'):
        values.append(f'gems={options["gems"]}')
    for kind in ('enchant', 'addon'):
        key = f'{kind}_id' if options.get(f'{kind}_id') else kind
        if options.get(key):
            values.append(f'{key}={options[key]}')
    return ','.join(values)


def prepare_control_input(code, binary, directory, *, execute=None):
    """执行两次仅初始化的原生导出；校验通过后才返回正式模拟输入。"""
    markers = [line[len(MARKER):] for line in code.splitlines() if line.startswith(MARKER)]
    if not markers:
        return code
    if len(markers) != 1:
        raise ValueError('装备特效对照标记重复')
    payload = json.loads(base64.urlsafe_b64decode(markers[0]).decode())
    if isinstance(payload, dict) and payload.get('version') == 2:
        return _prepare_combination(code, payload, binary, directory, execute=execute)
    if (not isinstance(payload, dict) or set(payload) != {'slot', 'value'}
            or payload['slot'] not in SLOTS or not isinstance(payload['value'], str)
            or '\n' in payload['value'] or '\r' in payload['value']):
        raise ValueError('装备特效对照标记无效')
    slot = ALIASES.get(payload['slot'], payload['slot'])
    original_lines = [line for line in code.splitlines() if not line.startswith(MARKER)]
    indexes = [i for i, line in enumerate(original_lines)
               if ALIASES.get(line.partition('=')[0], line.partition('=')[0]) == slot]
    if len(indexes) != 1 or original_lines[indexes[0]].partition('=')[2] != PENDING:
        raise ValueError('装备特效对照占位输入不匹配')
    index = indexes[0]
    original_lines[index] = f'{slot}={payload["value"]}'
    directory = Path(directory).resolve()
    prefix = 'equipment-control-' + uuid.uuid4().hex

    def probe(lines, suffix):
        input_path = directory / f'{prefix}-{suffix}.simc'
        profile_path = directory / f'{prefix}-{suffix}-saved.simc'
        log_path = directory / f'{prefix}-{suffix}.log'
        # 保存模式只初始化角色，不执行战斗；路径由执行器生成。
        input_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        command = [str(binary), str(input_path), f'save={profile_path}',
                   'save_gear_comments=1', 'save_profile_with_actions=0',
                   'debug=1', 'threads=1', f'output={log_path}']
        result = (execute(command) if execute else subprocess.run(
            command, cwd=directory, capture_output=True, timeout=60,
        ))
        if result.returncode or not profile_path.is_file() or not log_path.is_file():
            detail = str(result.stderr or result.stdout or '')[-2000:]
            raise ValueError(f'SimC 无特效对照初始化失败：{detail}')
        log = log_path.read_text(encoding='utf-8', errors='replace')
        if 'SimulationCraft has not been built with PTR data' in log:
            raise ValueError('当前 SimC 不支持所需 PTR 数据')
        return parse_equipment_export(profile_path.read_text(encoding='utf-8-sig'), log, slot)

    original = probe(original_lines, 'original')
    replacement = synthetic_item(original)
    control_lines = list(original_lines)
    control_lines[index] = f'{slot}={replacement}'
    control = probe(control_lines, 'control')
    for field in ('stats', 'weapon', 'attachments'):
        if original[field] != control[field]:
            raise ValueError(f'无特效对照的 {field} 与原装备不一致，拒绝生成收益')
    if re.search(r'\bsource=item\b', control['record']):
        raise ValueError('无特效对照仍存在装备自带效果')
    return '\n'.join(control_lines) + '\n'


def embellishment_count(export, rules):
    """结合原生效果、bonus、显式名称和目录身份，避免漏掉自带美化。"""
    options = export['options']
    name = options.get('embellishment', '').lower()
    if name and name != 'none' and name not in rules['embellishments']:
        if name not in ('alchemical_flavor_pocket', 'griftahs_allpurpose_embellishing_powder',
                        'griftahs_heavyduty_embellishing_powder'):
            raise ValueError(f'美化 {name} 缺少已核实规则，拒绝生成收益')
    bonus_ids = [int(value) for value in re.findall(r'\d+', options.get('bonus_id', ''))]
    known_bonus_ids = {row['bonus_id'] for row in rules['embellishments'].values()}
    declared = sum(value in known_bonus_ids for value in bonus_ids) + int(name in rules['embellishments'])
    proc_ids = [int(value) for value in re.findall(r'proc=\w+/(\d+)', export['record'])]
    native = sum(value in rules['effect_ids'] for value in proc_ids)
    intrinsic = int(options.get('id', '0')) in rules['intrinsic_item_ids']
    return max(native, declared + int(intrinsic))


def _prepare_combination(code, payload, binary, directory, *, execute):
    """先清理背景美化，再生成候选组；逐部位核验后才执行战斗。"""
    items, rules = payload.get('items'), payload.get('rules')
    if (set(payload) != {'version', 'items', 'control', 'rules'} or not isinstance(items, dict)
            or not items or any(slot not in SLOTS for slot in items)
            or any(not isinstance(value, str) or '\n' in value or '\r' in value for value in items.values())
            or type(payload['control']) is not bool or not isinstance(rules, dict)
            or rules.get('version') != 2):
        raise ValueError('装备组合冻结输入无效')
    validate_effect_policy({'candidate_type': 'gear_swap',
                           'gear_swaps': [{'slot': slot} for slot in items],
                           'equipment_effect_policy': {'version': 2, 'target_slots': list(items), 'rules': rules}})
    lines = [line for line in code.splitlines() if not line.startswith(MARKER)]
    indexes = {}
    for index, line in enumerate(lines):
        key, sep, value = line.partition('=')
        slot = ALIASES.get(key.strip(), key.strip())
        if sep and slot in ALL_SLOTS:
            if value.strip() in ('', 'none', 'empty', 'nothing') and slot not in items:
                continue
            if slot in indexes:
                raise ValueError('装备组合输入包含重复槽位')
            indexes[slot] = index
            if slot in items:
                if value != PENDING:
                    raise ValueError('装备组合占位输入不匹配')
                lines[index] = f'{slot}={items[slot]}'
    if not set(items).issubset(indexes):
        raise ValueError('装备组合输入缺少目标槽位')
    directory = Path(directory).resolve()
    prefix = 'equipment-combination-' + uuid.uuid4().hex

    def probe(content, suffix):
        input_path = directory / f'{prefix}-{suffix}.simc'
        profile_path = directory / f'{prefix}-{suffix}-saved.simc'
        log_path = directory / f'{prefix}-{suffix}.log'
        input_path.write_text('\n'.join(content) + '\n', encoding='utf-8')
        command = [str(binary), str(input_path), f'save={profile_path}', 'save_gear_comments=1',
                   'save_profile_with_actions=0', 'debug=1', 'threads=1', f'output={log_path}']
        result = execute(command) if execute else subprocess.run(command, cwd=directory, capture_output=True, timeout=60)
        if result.returncode or not profile_path.is_file() or not log_path.is_file():
            raise ValueError(f'SimC 装备组合初始化失败：{str(result.stderr or result.stdout or "")[-2000:]}')
        profile = profile_path.read_text(encoding='utf-8-sig')
        log = log_path.read_text(encoding='utf-8', errors='replace')
        if 'SimulationCraft has not been built with PTR data' in log:
            raise ValueError('当前 SimC 不支持所需 PTR 数据')
        return {slot: parse_equipment_export(profile, log, slot) for slot in indexes}

    original = probe(lines, 'original')
    counts = {slot: embellishment_count(item, rules) for slot, item in original.items()}
    if sum(counts[slot] for slot in items) > 2:
        raise ValueError('候选组合超过两件美化或在同一装备上叠加了多个美化')
    if any(counts[slot] > 1 for slot in items):
        raise ValueError('单件候选同时包含多个美化来源')
    removed = {slot for slot, count in counts.items() if count and slot not in items}
    if payload['control']:
        removed.update(items)
    for slot in removed:
        lines[indexes[slot]] = f'{slot}={synthetic_item(original[slot])}'
    # 只覆盖此次移除装备所属的非职业套装，职业套装显式配置保持一致。
    removed_ids = {int(original[slot]['options'].get('id', '0')) for slot in removed}
    remaining_ids = [int(item['options'].get('id', '0')) for slot, item in original.items() if slot not in removed]
    for row in rules['sets']:
        if (removed_ids.intersection(row['items'])
                and sum(item_id in row['items'] for item_id in remaining_ids) < row['pieces']):
            lines.append(f'set_bonus=name={row["name"]},pc={row["pieces"]},enable=0')
    prepared = probe(lines, 'prepared')
    for slot, before in original.items():
        for field in ('stats', 'weapon', 'attachments'):
            if before[field] != prepared[slot][field]:
                raise ValueError(f'装备组合的 {slot} {field} 不一致，拒绝生成收益')
        if slot in removed and re.search(r'\bsource=item\b', prepared[slot]['record']):
            raise ValueError(f'无特效装备 {slot} 仍包含自带效果')
    actual = {slot: embellishment_count(item, rules) for slot, item in prepared.items()}
    expected = 0 if payload['control'] else sum(counts[slot] for slot in items)
    if sum(actual.values()) != expected or sum(actual.values()) > 2:
        raise ValueError('最终装备美化数量与目标组合不一致')
    return '\n'.join(lines) + '\n'
