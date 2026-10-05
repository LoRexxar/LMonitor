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
EXPECTATION_MARKER = '# lmonitor_equipment_expectation_v1='
NATIVE_PROOF_MARKER = '# lmonitor_equipment_native_proof_v1='
NATIVE_PROOF_MAX_BYTES = 64 * 1024


def native_proof_marker(proof):
    encoded = json.dumps(proof, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > NATIVE_PROOF_MAX_BYTES:
        raise ValueError('装备原生证据超过大小上限')
    return NATIVE_PROOF_MARKER + encoded + '\n'


def extract_native_proof(text):
    """Strict bounded comment/stdout protocol; absent evidence stays absent."""
    matches = [line for line in text.splitlines() if line.startswith(NATIVE_PROOF_MARKER)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('装备原生证据标记重复')
    encoded = matches[0][len(NATIVE_PROOF_MARKER):]
    if len(encoded.encode('utf-8')) > NATIVE_PROOF_MAX_BYTES:
        raise ValueError('装备原生证据超过大小上限')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('装备原生证据字段重复')
            result[key] = value
        return result

    try:
        proof = json.loads(encoded, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite proof')))
        if not isinstance(proof, dict):
            raise ValueError('装备原生证据格式无效')
        return proof
    except (TypeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError('装备原生证据格式无效') from exc
NATIVE_SLOTS = frozenset(('head', 'neck', 'shoulders', 'back', 'chest', 'wrists',
                   'hands', 'waist', 'legs', 'feet', 'finger1', 'finger2',
                   'main_hand', 'off_hand'))
ALIASES = {'shoulder': 'shoulders', 'wrist': 'wrists', 'hand': 'hands',
           'ring1': 'finger1', 'ring2': 'finger2'}
ALL_SLOTS = NATIVE_SLOTS | {'trinket1', 'trinket2'}
# 组合可跨武器和饰品槽；旧单件对照入口仍使用 NATIVE_SLOTS。
# 应用快照与原生 SimC 的槽位拼写不同，入口统一接受这些别名。
SLOTS = ALL_SLOTS | frozenset(ALIASES)
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
    if isinstance(policy, dict) and policy.get('version') == 3:
        from simc_equipment_conditional import validate_contract
        # Composition checks structure only. Independent approval is still
        # mandatory at prepare; never derive authorization from candidate data.
        validate_contract(policy, params.get('equipment_effect_expectation'),
                          require_authorization=False)
        if params.get('candidate_type') != 'gear_swap' or policy['target_slots'] != slots:
            raise ValueError('conditional candidate slots mismatch')
        return
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


def mark_equipment_input(code, slots, *, control, rules, expectation=None, policy=None):
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
    if policy is not None:
        from simc_equipment_conditional import validate_contract
        validate_contract(policy, expectation, require_authorization=False)
        if policy['target_slots'] != slots or policy['rules'] != rules:
            raise ValueError('conditional marker policy mismatch')
        payload.update(version=3, policy=policy,
                       items={slot: originals[slot] for slot in policy['target_slots']})
    encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    result = '\n'.join(lines) + '\n' + MARKER + encoded + '\n'
    if expectation is not None:
        validate_equipment_expectation(expectation, slots)
        result += EXPECTATION_MARKER + base64.urlsafe_b64encode(json.dumps(expectation).encode()).decode() + '\n'
    return result


def validate_weapon_layout(params):
    """Bind frozen weapon facts to this exact candidate, without live lookups."""
    layout = params.get('equipment_weapon_layout')
    if (not isinstance(layout, dict) or set(layout) != {'version', 'weapons', 'titan_grip'}
            or type(layout['version']) is not int or layout['version'] != 1
            or type(layout['titan_grip']) is not bool or not isinstance(layout['weapons'], list)):
        raise ValueError('冻结武器布局无效')
    swaps = {ALIASES.get(row.get('slot'), row.get('slot')): row for row in candidate_swaps(params)
             if row.get('slot') in ('main_hand', 'off_hand')}
    seen = set()
    for row in layout['weapons']:
        if (not isinstance(row, dict) or set(row) != {'slot', 'item_id', 'inventory_type'}
                or row['slot'] not in swaps or row['slot'] in seen
                or type(row['item_id']) is not int or row['item_id'] <= 0
                or type(row['inventory_type']) is not int or row['inventory_type'] <= 0):
            raise ValueError('冻结武器部位事实无效')
        swap = swaps[row['slot']]
        options = _options(str(swap.get('raw_value') or ''))
        if str(row['item_id']) != str(options.get('id') or swap.get('item_id')):
            raise ValueError('冻结武器与候选物品身份不一致')
        seen.add(row['slot'])
    if not seen or seen != set(swaps):
        raise ValueError('冻结武器布局缺少候选槽位')
    clear = not layout['titan_grip'] and any(
        row['slot'] == 'main_hand' and row['inventory_type'] == 17 for row in layout['weapons'])
    if clear and 'off_hand' in swaps:
        raise ValueError('双手主手不能同时配置副手装备')
    return clear


def apply_weapon_layout(equipment, params):
    if 'equipment_weapon_layout' not in params:
        return equipment  # Historical tasks retain their frozen semantics.
    clear = validate_weapon_layout(params)
    paired = {row['slot'] for row in params['equipment_weapon_layout']['weapons']} == {'main_hand', 'off_hand'}
    if not clear and not paired:
        return equipment
    lines = []
    in_candidate_section = False
    has_offhand = False
    base_end = None
    for line in str(equipment or '').splitlines():
        if line.strip().startswith('###'):
            if not in_candidate_section:
                base_end = len(lines)
            in_candidate_section = True
        if not in_candidate_section and line.partition('=')[0].strip().lower() == 'off_hand':
            has_offhand = True
            if clear:
                line = 'off_hand='
        lines.append(line)
    if paired and not has_offhand:
        # Only an explicit, identity-validated pair authorizes a missing slot.
        lines.insert(base_end if base_end is not None else len(lines), 'off_hand=')
    return '\n'.join(lines)


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


def parse_equipment_export(profile, log, slot, *, schema_version=2):
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
            'effect_log': native_effect_log(log),
            'effects': native_item_effects(lines[index].partition('=')[2], log, slot, schema_version=schema_version),
            'attachments': {key: blocks.get(key, '') for key in
                            ('gems', 'enchant', 'addon', 'temporary_enchant')},
            'record': record, 'profile_value': lines[index].partition('=')[2]}


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


def prepare_control_input(code, binary, directory, *, execute=None, conditional_authorization=None):
    """执行两次仅初始化的原生导出；校验通过后才返回正式模拟输入。"""
    markers = [line[len(MARKER):] for line in code.splitlines() if line.startswith(MARKER)]
    if not markers:
        return code
    if len(markers) != 1:
        raise ValueError('装备特效对照标记重复')
    payload = json.loads(base64.urlsafe_b64decode(markers[0]).decode())
    if isinstance(payload, dict) and payload.get('version') in (2, 3):
        expected = [line[len(EXPECTATION_MARKER):] for line in code.splitlines() if line.startswith(EXPECTATION_MARKER)]
        if len(expected) > 1:
            raise ValueError('装备激活期待标记重复')
        expectation = json.loads(base64.urlsafe_b64decode(expected[0]).decode()) if expected else None
        if expectation is not None:
            validate_equipment_expectation(expectation, list(payload.get('items', {})))
        return _prepare_combination(code, payload, binary, directory, execute=execute, expectation=expectation,
                                    conditional_authorization=conditional_authorization)
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
    _require_target_effect({slot: original}, [slot], frozenset())
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


def _item_effects(export):
    """Only instantiated item effects count; DB proc_spells and attachments do not."""
    effects = []
    for block in re.findall(r'\beffect=\{\s*([^{}]*?)\s*\}', export['record']):
        fields = dict(re.findall(r'\b(\w+)=([^\s]+)', block))
        driver = fields.get('driver', '')
        if (fields.get('source') == 'item' and fields.get('type') in ('equip', 'use')
                and driver.isdecimal() and int(driver) > 0):
            effects.append((fields['type'], int(driver)))
    return tuple(sorted(effects))


def native_effect_log(log):
    """Keep the native origin chain, including competing mappings, without APLs."""
    return '\n'.join(line for line in log.splitlines() if
        ' adding effect ' in line or 'Initializing items for Player ' in line
        or 'Initializing special effects for Player ' in line
        or 'Creating Auras, Buffs, and Debuffs for Pet ' in line
        or 'Initializing item-based special effect ' in line
        or (' name=' in line and ' slot=' in line and ' source=' in line))


def native_item_effects(profile_value, log, slot, *, schema_version=2):
    """Select provenance semantics explicitly; never reinterpret frozen v2 proof."""
    if type(schema_version) is not int or schema_version not in (2, 3):
        raise ValueError('unsupported native origin schema')
    parser = _native_item_effects_v2 if schema_version == 2 else _native_item_effects_v3
    return parser(profile_value, log, slot)


def _native_item_effects_v2(profile_value, log, slot):
    """Bind actual effect blocks to an unambiguous native item/actor origin.

    Final item records have no actor label. Only a single native actor scope can
    therefore authorize migration. Item names bind adding lines to unique final
    records; effect names and proc_spells never establish identity or activation.
    Repeated identical initialization pairs (shared copies) are allowed, but
    competing triggers, declarations, or final blocks cannot authorize a binding.
    """
    slot = ALIASES.get(slot, slot)
    lines = log.splitlines()
    records = [line for line in lines if ' name=' in line and ' slot=' in line and ' source=' in line]
    selected = [line for line in records if re.search(rf'\bslot={re.escape(slot)}\s', line)]
    if len(selected) != 1:
        raise ValueError('native effect record ambiguous or missing')
    record = selected[0]
    effects = []
    for block in re.findall(r'\beffect=\{\s*([^{}]*?)\s*\}', record):
        fields = dict(re.findall(r'\b(\w+)=([^\s]+)', block))
        driver, trigger = fields.get('driver', ''), fields.get('trigger', '')
        if (fields.get('source') == 'item' and fields.get('type') in ('equip', 'use')
                and driver.isdecimal() and int(driver) > 0):
            effects.append({'source': 'item', 'type': fields['type'], 'driver': int(driver),
                            'trigger': int(trigger) if trigger.isdecimal() and int(trigger) > 0 else None,
                            'origin': None})
    item_scopes = re.findall(r"Initializing items for Player '([^']+)'\.", log)
    effect_scopes = re.findall(r"Initializing special effects for Player '([^']+)'\.", log)
    # SimC also labels pet item initialization as Player. Exclude only actors
    # explicitly identified as pets, never by name, and only without their own
    # item effects. The single equipped actor/origin requirement stays intact.
    pets = set(re.findall(r"Creating Auras, Buffs, and Debuffs for Pet '([^']+)'\.", log))
    if pets.intersection(re.findall(r"Player (\S+) item '[^']+' adding effect ", log)):
        return effects
    item_scopes = [actor for actor in item_scopes if actor not in pets]
    identity = re.search(r'\bname=(\S+) id=(\d+) slot=(\S+)', record)
    if (len(item_scopes) != 1 or effect_scopes != item_scopes or not identity
            or identity[2] != _options(profile_value).get('id')
            or sum(bool(re.search(rf'\bname={re.escape(identity[1])}\s', row)) for row in records) != 1):
        return effects
    actor, name = item_scopes[0], identity[1]
    additions = []
    for line in lines:
        match = re.search(r"Player (\S+) item '([^']+)' adding effect (\d+) \(type=(\w+), index=(\d+)\)", line)
        if match and match[1] == actor and match[2] == name:
            additions.append((match[4], int(match[3]), int(match[5])))
    if len({index for _, _, index in additions}) != len(additions):
        return effects
    mappings = {}
    in_actor = False
    for line in lines:
        scope = re.search(r"Initializing special effects for Player '([^']+)'\.", line)
        if scope:
            in_actor = scope[1] == actor
        if in_actor and 'Initializing item-based special effect ' in line:
            fields = dict(re.findall(r'\b(\w+)=([^\s]+)', line))
            if fields.get('source') == 'item':
                mappings.setdefault((fields.get('type'), fields.get('driver')), set()).add(fields.get('trigger'))
    candidates = []
    for effect in effects:
        matches = []
        for kind, driver, index in additions:
            triggers = mappings.get((kind, str(driver)), set())
            if (effect['type'] == kind and effect['trigger'] is not None
                    and triggers == {str(effect['trigger'])}
                    and effect['driver'] in (driver, effect['trigger'])):
                matches.append({'actor': actor, 'slot': slot, 'item_id': int(identity[2]),
                                'index': index, 'type': kind, 'driver': driver, 'trigger': effect['trigger']})
        candidates.append(matches)
    for effect, matches in zip(effects, candidates):
        if len(matches) == 1 and sum(matches[0] in other for other in candidates) == 1:
            effect['origin'] = matches[0]
    return effects


def _native_item_effects_v3(profile_value, log, slot):
    """Prove D/T -> D/T or T/U, using the entire native item roster.

    Attachment declarations bind shared copies to their own item, but competing
    numeric identities never do. With no attachment, a single raw initialization
    instance and a bidirectionally unique final instance are mandatory. No index
    is invented and neither names of effects nor expected drivers create edges.
    """
    slot = ALIASES.get(slot, slot)
    # Preserve the runtime projection, not v2's optional attribution.
    effects = _native_item_effects_v2(profile_value, log, slot)
    for effect in effects:
        effect['origin'] = None
    lines = log.splitlines()
    records = [line for line in lines if ' name=' in line and ' slot=' in line and ' source=' in line]
    identities = [re.search(r'\bname=(\S+) id=(\d+) slot=(\S+)', row) for row in records]
    if any(identity is None for identity in identities):
        return effects
    identities = [identity.groups() for identity in identities]
    selected = [i for i, (_, _, s) in enumerate(identities) if s == slot]
    if len(selected) != 1:
        return effects
    selected_index = selected[0]
    name, item_id, _ = identities[selected_index]
    if (item_id != _options(profile_value).get('id') or int(item_id) <= 0
            or profile_value.partition(',')[0] != name
            or sum(n == name for n, _, _ in identities) != 1
            or len({s for _, _, s in identities}) != len(identities)):
        return effects
    # Native names are unescaped: an apostrophe is data until the closing
    # quote-plus-period at line end. Keep full names for exact actor/pet checks.
    # A malformed boundary must not disappear from the roster or leave the
    # preceding actor authorized, even if it occurs after the last item init.
    for line in lines:
        for marker in ('Initializing items for Player ',
                       'Initializing special effects for Player ',
                       'Creating Auras, Buffs, and Debuffs for Pet '):
            if marker in line and not re.fullmatch(r"'[^\r\n]+'\.", line.partition(marker)[2]):
                return effects
    pets = set(re.findall(r"Creating Auras, Buffs, and Debuffs for Pet '([^\r\n]+)'\.$", log, re.MULTILINE))
    actors = [a for a in re.findall(r"Initializing items for Player '([^\r\n]+)'\.$", log, re.MULTILINE) if a not in pets]
    scope_pattern = re.compile(r"Initializing special effects for Player '([^\r\n]+)'\.$", re.MULTILINE)
    scopes = scope_pattern.findall(log)
    if len(actors) != 1 or scopes != actors:
        return effects
    actor = actors[0]
    declarations = []
    for line in lines:
        match = re.search(r"Player (\S+) item '([^']+)' adding effect (\d+) \(type=(\w+), index=(\d+)\)", line)
        if match:
            if match[1] != actor:
                return effects
            declarations.append((match[2], match[4], int(match[3]), int(match[5])))
    if len({(n, index) for n, _, _, index in declarations}) != len(declarations):
        return effects

    def pair(line):
        fields = dict(re.findall(r'\b(\w+)=([^\s]+)', line))
        driver, trigger = fields.get('driver', ''), fields.get('trigger', '')
        if (fields.get('source') == 'item' and fields.get('type') in ('equip', 'use')
                and driver.isdecimal() and int(driver) > 0):
            return (fields['type'], int(driver),
                    int(trigger) if trigger.isdecimal() and int(trigger) > 0 else None)
        return None

    initials = []
    in_actor = False
    for line in lines:
        scope = scope_pattern.search(line)
        if scope:
            in_actor = scope[1] == actor
        if 'Initializing item-based special effect ' in line:
            value = pair(line)
            if value:
                if not in_actor:
                    return effects
                initials.append(value)  # Keep raw instance multiplicity.
    finals = []
    for i, record in enumerate(records):
        for block in re.findall(r'\beffect=\{\s*([^{}]*?)\s*\}', record):
            value = pair(block)
            if value:
                finals.append((i, value))

    def linked(initial, final):
        kind, driver, trigger = initial
        return (kind == final[0] and trigger is not None and final[2] is not None
                and ((driver, trigger) == final[1:] or trigger == final[1]))

    unique_initials = set(initials)
    candidates = [{initial for initial in unique_initials if linked(initial, final)} for _, final in finals]
    target_effects = iter(effects)
    for j, (record_index, final) in enumerate(finals):
        if record_index != selected_index:
            continue
        effect = next(target_effects)
        if len(candidates[j]) != 1:
            continue
        initial = next(iter(candidates[j]))
        kind, driver, trigger = initial
        if {t for k, d, t in initials if (k, d) == (kind, driver)} != {trigger}:
            continue
        outgoing = [n for n, (_, value) in enumerate(finals) if linked(initial, value)]
        declared = [d for d in declarations if d[1:3] == (kind, driver)]
        own = [d for d in declarations if d[0] == name]
        origin = {'actor': actor, 'slot': slot, 'item_id': int(item_id),
                  'type': kind, 'driver': driver, 'trigger': trigger}
        if declared:
            # Check both directions across ALL records, not just target effects.
            # Multiple identical initial pairs are legitimate only when each has
            # its own explicit attachment and exactly one corresponding final.
            bound = []
            for declaration in declared:
                item_name = declaration[0]
                matches = [n for n in outgoing if identities[finals[n][0]][0] == item_name]
                if (len(matches) != 1 or len(candidates[matches[0]]) != 1
                        or sum(n == item_name for n, _, _ in identities) != 1):
                    break
                bound.append(matches[0])
            else:
                matching = [d for d in own if d in declared]
                if (len(matching) == 1 and len(bound) == len(set(bound))
                        and set(bound) == set(outgoing) and initials.count(initial) == len(declared)):
                    origin.update(source='native_attachment', index=matching[0][3])
                    effect['origin'] = origin
        elif not own and initials.count(initial) == 1 and outgoing == [j]:
            origin['source'] = 'native_initialization_chain'
            effect['origin'] = origin
    return effects


def native_effects_equal(before, after):
    """Keep exact runtime equality; migration requires origins on both sides."""
    def runtime(rows):
        return sorted((row['type'], row['driver'], row.get('trigger') or 0) for row in rows)
    if runtime(before) == runtime(after):
        return True
    def identities(rows):
        return sorted(json.dumps(row['origin'], sort_keys=True) if row.get('origin') else
                      json.dumps({'runtime': [row['type'], row['driver'], row.get('trigger')]}, sort_keys=True)
                      for row in rows)
    return identities(before) == identities(after)


def native_effect_drivers(effects):
    # A central declared driver may be proven by the origin chain, never by a
    # trigger alone. The final instantiated block remains mandatory.
    return {row['driver'] for row in effects} | {
        row['origin']['driver'] for row in effects if row.get('origin')}


def _native_sets(log):
    """Read the native initialized roster, not saved comments or declared rules."""
    result = set()
    for line in log.splitlines():
        if 'Initialized set bonus:' not in line:
            continue
        matches = re.findall(
            r'\{\s*[^,{}]+,\s*([a-z0-9_]+),\s*[^,{}]+,\s*(\d+) piece bonus[^{}]*\}', line)
        if not matches:
            raise ValueError('原生套装初始化记录无法识别，拒绝生成收益')
        for name, pieces in matches:
            result.add((name, int(pieces)))
    return frozenset(result)


def _target_sets(exports, slots, rules, native_sets):
    ids = [int(item['options'].get('id', '0')) for item in exports.values()]
    target_ids = {int(exports[slot]['options'].get('id', '0')) for slot in slots}
    return {(row['name'], row['pieces']) for row in rules['sets']
            if (row['name'], row['pieces']) in native_sets
            and target_ids.intersection(row['items'])
            and sum(item_id in row['items'] for item_id in ids) >= row['pieces']}


def _require_target_effect(exports, slots, active_sets):
    if not any(_item_effects(exports[slot]) for slot in slots) and not active_sets:
        identities = ', '.join(f'{slot}(id={exports[slot]["options"].get("id", "0")})' for slot in slots)
        raise ValueError(f'目标装备 {identities} 未加载原生有效自带特效或已核实套装效果，拒绝生成收益')


def validate_equipment_expectation(expectation, slots):
    if isinstance(expectation, dict) and expectation.get('schema_version') == 2:
        # Full policy/trust validation is mandatory at conditional preparation.
        return validate_equipment_expectation({'schema_version': 1, 'targets': expectation.get('targets')}, slots)
    if (not isinstance(expectation, dict) or set(expectation) != {'schema_version', 'targets'}
            or type(expectation.get('schema_version')) is not int or expectation['schema_version'] != 1
            or not isinstance(expectation['targets'], list)
            or not expectation['targets'] or len(expectation['targets']) > len(slots)):
        raise ValueError('装备激活期待格式无效')
    seen = set()
    for row in expectation['targets']:
        if (not isinstance(row, dict) or row.get('slot') not in slots or row['slot'] in seen
                or type(row.get('item_id')) is not int or row['item_id'] <= 0
                or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', str(row.get('game_build', '')))
                or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('fact_hash', '')))):
            raise ValueError('装备激活期待身份无效')
        seen.add(row['slot'])
        for key in ('required_bonus_ids', 'driver_spell_ids', 'event_spell_ids'):
            values = row.get(key)
            if not isinstance(values, list) or any(type(value) is not int or value <= 0 for value in values):
                raise ValueError('装备激活期待 ID 无效')


def _require_declared_effects(exports, expectation):
    for row in (expectation or {}).get('targets', []):
        item = exports[row['slot']]
        drivers = native_effect_drivers(item['effects'])
        bonuses = {int(value) for value in re.findall(r'\d+', item['options'].get('bonus_id', ''))}
        if (int(item['options'].get('id', '0')) != row['item_id']
                or not set(row['required_bonus_ids']).issubset(bonuses)
                or not set(row['driver_spell_ids']).issubset(drivers)):
            raise ValueError(f'目标装备 {row["slot"]} 未加载中央预期特效，拒绝生成收益')


def _prepare_combination(code, payload, binary, directory, *, execute, expectation=None, conditional_authorization=None):
    """先清理背景美化，再生成候选组；逐部位核验后才执行战斗。"""
    items, rules = payload.get('items'), payload.get('rules')
    conditional = payload.get('version') == 3
    if conditional:
        from simc_equipment_conditional import validate_contract, bind_binary, authorize_exports, digest, input_digest
        relation = validate_contract(payload['policy'], expectation, authorization=conditional_authorization)
        if payload['policy']['rules'] != rules or payload['policy']['target_slots'] != list(items):
            raise ValueError('conditional marker mismatch')
        bind_binary(binary, expectation)
        changed = set(payload['policy']['changed_slots'])
        context = set(payload['policy']['context_slots'])
        policy = payload['policy']
        payload = {key: value for key, value in payload.items() if key != 'policy'}
    else:
        if (expectation or {}).get('schema_version') == 2:
            raise ValueError('conditional expectation requires conditional policy')
        changed, context = set(items or {}), set()
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
        return {slot: parse_equipment_export(profile, log, slot, schema_version=3) for slot in indexes}, _native_sets(log)

    original, original_sets = probe(lines, 'original')
    if conditional:
        authorize_exports(original, expectation, relation)
    _require_declared_effects(original, expectation)
    counts = {slot: embellishment_count(item, rules) for slot, item in original.items()}
    if sum(counts[slot] for slot in items) > 2:
        raise ValueError('候选组合超过两件美化或在同一装备上叠加了多个美化')
    if any(counts[slot] > 1 for slot in items):
        raise ValueError('单件候选同时包含多个美化来源')
    _require_target_effect(original, items, _target_sets(original, items, rules, original_sets))
    background = {slot for slot, count in counts.items() if count and slot not in items}

    def without_effects(removed):
        content = list(lines)
        for slot in removed:
            content[indexes[slot]] = f'{slot}={synthetic_item(original[slot])}'
        # Only disable the affected non-class sets, leaving class overrides intact.
        removed_ids = {int(original[slot]['options'].get('id', '0')) for slot in removed}
        remaining_ids = [int(item['options'].get('id', '0')) for slot, item in original.items() if slot not in removed]
        for row in rules['sets']:
            if (removed_ids.intersection(row['items'])
                    and sum(item_id in row['items'] for item_id in remaining_ids) < row['pieces']):
                content.append(f'set_bonus=name={row["name"]},pc={row["pieces"]},enable=0')
        return content

    normal_lines = without_effects(background)
    normal, normal_sets = probe(normal_lines, 'normal-prepared') if background else (original, original_sets)
    control_lines = without_effects(background | changed)
    control, control_sets = probe(control_lines, 'prepared')
    known_sets = {(row['name'], row['pieces']) for row in rules['sets']}
    changed_sets = (original_sets ^ normal_sets) | (normal_sets ^ control_sets)
    unknown_sets = changed_sets - known_sets
    if unknown_sets:
        names = ', '.join(f'{name}/{pieces}pc' for name, pieces in sorted(unknown_sets))
        raise ValueError(f'装备套装 {names} 缺少已核实规则，拒绝生成收益')
    removed_ids = {int(original[slot]['options'].get('id', '0')) for slot in background | changed}
    remaining_ids = [int(item['options'].get('id', '0')) for slot, item in original.items()
                     if slot not in background and slot not in changed]
    for row in rules['sets']:
        if (removed_ids.intersection(row['items'])
                and sum(item_id in row['items'] for item_id in remaining_ids) < row['pieces']
                and (row['name'], row['pieces']) in control_sets):
            raise ValueError(f'无特效对照套装 {row["name"]} 仍初始化，拒绝生成收益')
    # A rule alone does not prove activation. The native set must initialize in
    # normal and disappear when the target is stripped, tying it to this candidate.
    target_sets = _target_sets(normal, items, rules, normal_sets) & (normal_sets - control_sets)
    _require_target_effect(normal, items, target_sets)
    if conditional:
        authorize_exports(normal, expectation, relation)
        for slot in context:
            if (normal[slot]['profile_value'] != control[slot]['profile_value']
                    or not native_effects_equal(normal[slot]['effects'], control[slot]['effects'])):
                raise ValueError('conditional consumer changed')
    _require_declared_effects(normal, expectation)
    for prepared, removed in ((normal, background), (control, background | changed)):
        for slot, before in original.items():
            for field in ('stats', 'weapon', 'attachments'):
                if before[field] != prepared[slot][field]:
                    raise ValueError(f'装备组合的 {slot} {field} 不一致，拒绝生成收益')
            if slot in removed and re.search(r'\bsource=item\b', prepared[slot]['record']):
                raise ValueError(f'无特效装备 {slot} 仍包含自带效果')
        actual = {slot: embellishment_count(item, rules) for slot, item in prepared.items()}
        expected = sum(counts[slot] for slot in context) if prepared is control else sum(counts[slot] for slot in items)
        if sum(actual.values()) != expected or sum(actual.values()) > 2:
            raise ValueError('最终装备美化数量与目标组合不一致')
    for slot in items:
        if (original[slot]['profile_value'] != normal[slot]['profile_value']
                or not native_effects_equal(original[slot]['effects'], normal[slot]['effects'])):
            raise ValueError(f'目标装备 {slot} 原生效果与准备后不一致，拒绝生成收益')
    # Persist only after every native preparation gate has passed. Full paired
    # static/effect snapshots are evidence, not a bare success flag.
    def snapshot(exports, sets):
        return {'items': {slot: {
            'item_id': int(item['options'].get('id', '0')),
            'bonus_ids': sorted({int(value) for value in re.findall(r'\d+', item['options'].get('bonus_id', ''))}),
            'profile_value': item['profile_value'],
            'static': {key: item[key] for key in ('stats', 'weapon', 'attachments')},
            'effects': item['effects'],
        } for slot, item in exports.items()}, 'sets': [list(row) for row in sorted(sets)],
                'effect_log': next(iter(exports.values()))['effect_log']}

    proof = {'schema_version': 3, 'scope': 'equipment_effect_combination',
             'mode': 'control' if payload['control'] else 'normal',
             'targets': [{'slot': slot, 'item_id': int(original[slot]['options'].get('id', '0'))}
                         for slot in items],
             'background_removed': sorted(background),
             'rules_hash': hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
             'original': snapshot(original, original_sets),
             'normal': snapshot(normal, normal_sets), 'control': snapshot(control, control_sets),
             'target_sets': [list(row) for row in sorted(target_sets)]}
    prepared_lines = control_lines if payload['control'] else normal_lines
    if conditional:
        proof.update(schema_version=4, comparison_kind='conditional_increment',
            policy=policy, expectation=expectation,
            contract_hash=digest({'policy':policy,'expectation':expectation}),
            identity=expectation['identity'],
            input_hashes={'normal':input_digest('\n'.join(normal_lines)), 'control':input_digest('\n'.join(control_lines))})
        proof['pair_hash'] = digest({k:v for k,v in proof.items() if k != 'mode'})
    if any(line.startswith(NATIVE_PROOF_MARKER) for line in prepared_lines):
        raise ValueError('装备输入不允许预置原生证据')
    return '\n'.join(prepared_lines) + '\n' + native_proof_marker(proof)
