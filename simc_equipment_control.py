"""用原版 SimC 的装备导出生成并核验同属性、无自带特效的装备对照。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

MARKER = '# lmonitor_equipment_control_v1='
NATIVE_SLOTS = frozenset(('head', 'neck', 'shoulders', 'back', 'chest', 'wrists',
                   'hands', 'waist', 'legs', 'feet', 'finger1', 'finger2',
                   'main_hand', 'off_hand'))
ALIASES = {'shoulder': 'shoulders', 'wrist': 'wrists', 'hand': 'hands',
           'ring1': 'finger1', 'ring2': 'finger2'}
# Application snapshots use wrist/shoulder; native SimC exports use plurals.
# All configuration/worker gates consume this same accepted-slot set.
SLOTS = NATIVE_SLOTS | frozenset(ALIASES)
PENDING = 'lmonitor_effect_control_pending,stats=lmonitor_unprepared'


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
    if 'stats' not in blocks:
        raise ValueError('SimC 未导出装备基础属性')
    stats = _stats(blocks['stats'])
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
