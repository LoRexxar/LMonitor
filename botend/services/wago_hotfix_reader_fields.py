"""Reader-facing Hotfix columns; DB2 client rows are context, not live predecessors."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


FIELDS = {
    'spellname': [('Name_lang', '名称')],
    'spelleffect': [
        ('Effect', '效果类型'), ('ImplicitTarget_0', '效果目标'),
        ('EffectMiscValue_0', '其他数值[0]'),
        ('EffectBasePointsF', '基础数值'), ('EffectBasePoints', '基础数值'),
        ('EffectBonusCoefficient', '法强系数'),
        ('BonusCoefficientFromAP', '攻强系数'), ('PvpMultiplier', 'PvP 系数'),
        ('EffectAura', '光环类型'),
    ],
    'spellcooldowns': [('RecoveryTime', '冷却时间'), ('CategoryRecoveryTime', '共享冷却')],
    'spellmisc': [('RangeIndex', '范围索引'), ('CastingTimeIndex', '施法时间索引'),
                  ('SchoolMask', '法术类型')],
    'itemsparse': [('Display_lang', '物品名称'), ('ItemLevel', '物品等级'),
                   ('InventoryType', '装备栏位类型'), ('SellPrice', '售出价格'),
                   ('BuyPrice', '购买价格')],
    'traitdefinition': [('OverrideName_lang', '天赋名称'), ('SpellID', '关联技能ID'),
                        ('Description_lang', '天赋说明')],
    'spelltargetrestrictions': [('MaxTargets', '最大目标数'),
                                ('ConeDegrees', '锥形角度')],
}

# Blizzard spell enums as recorded in SimulationCraft data_enums.hh:
# E_LOOT=189, E_APPLY_AURA=6, T_UNIT_CASTER=1. These label configuration.
EFFECT_LABELS = {
    '2': '造成伤害', '3': '虚拟效果', '6': '施加光环',
    '10': '治疗', '31': '武器百分比伤害', '189': '拾取',
}
TARGET_LABELS = {
    '1': '施法者', '6': '敌方目标', '18': '施法者位置',
    '21': '友方目标', '25': '任意目标',
}
AURA_LABELS = {
    '218': '标签百分比修正', '430': '播放场景',
    '649': '标签 PvP 倍率百分比修正',
}

# SpellMisc.Attributes_{group} positions: https://wowdev.wiki/Spell.dbc/Attributes
CAST_PERMISSION_FLAGS = {
    (0, 24): '骑乘时施放', (0, 27): '坐姿时施放',
    (1, 5): '不打破潜行',
    (2, 14): '隐形时施放', (2, 19): '非变形形态施放',
    (4, 7): '施法中施放',
    (5, 3): '昏迷时施放', (5, 17): '恐惧时施放',
    (5, 18): '混乱时施放',
    (6, 12): '乘坐载具时施放',
    (9, 20): '引导中施放',
}


def _spell_misc_permission_changes(after, baseline):
    """Known changed positions only; no baseline means no change claim."""
    if not isinstance(after, dict) or not isinstance(baseline, dict) or not baseline:
        return [], []
    added, removed = [], []
    for position in CAST_PERMISSION_FLAGS:
        group, bit = position
        key = f'Attributes_{group}'
        if key not in after or key not in baseline:
            continue
        try:
            old, new = int(str(baseline[key])), int(str(after[key]))
        except (TypeError, ValueError):
            continue
        if not (0 <= old <= 0xFFFFFFFF and 0 <= new <= 0xFFFFFFFF):
            continue
        if bool(old & (1 << bit)) and not bool(new & (1 << bit)):
            removed.append(position)
        elif bool(new & (1 << bit)) and not bool(old & (1 << bit)):
            added.append(position)
    return removed, added


def spell_misc_permission_deltas(after, baseline):
    """Full known per-bit description shown with the physical DB2 row."""
    removed, added = _spell_misc_permission_changes(after, baseline)
    parts = []
    if removed:
        parts.append('移除以下施放相关标志：' + '、'.join(CAST_PERMISSION_FLAGS[pos] for pos in removed))
    if added:
        parts.append('新增以下施放相关标志：' + '、'.join(CAST_PERMISSION_FLAGS[pos] for pos in added))
    return '；'.join(parts)


def spell_misc_permission_brief(after, baseline):
    """Group a long, verified flag list in the object headline only."""
    removed, added = _spell_misc_permission_changes(after, baseline)
    if len(removed) + len(added) <= 3:
        return spell_misc_permission_deltas(after, baseline)
    categories = (
        ('骑乘/载具/坐姿', {(0, 24), (0, 27), (6, 12)}),
        ('施法/引导中', {(4, 7), (9, 20)}),
        ('昏迷/恐惧/混乱', {(5, 3), (5, 17), (5, 18)}),
        ('潜行/隐形/形态相关', {(1, 5), (2, 14), (2, 19)}),
    )
    parts = []
    for positions, verb in ((removed, '移除'), (added, '新增')):
        if not positions:
            continue
        labels = [label for label, group in categories if set(positions) & group]
        parts.append(f"{verb}{'、'.join(labels)}等施放相关标志（逐位见下方）")
    return '；'.join(parts)


def _same(left, right):
    if str(left) == str(right):
        return True
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError, TypeError):
        return False


def _value(field, raw):
    text = str(raw).strip()
    if field in ('SellPrice', 'BuyPrice') and text.isdecimal():
        amount = int(text)
        gold, remainder = divmod(amount, 10000)
        silver, copper = divmod(remainder, 100)
        return (f'{gold}金' if gold else '') + (f'{silver}银' if silver else '') + (f'{copper}铜' if copper else '') or '0铜'
    def readable_number(number, *, places='0.01'):
        if not number.is_finite():
            return text
        rounded = number.quantize(Decimal(places), rounding=ROUND_HALF_UP)
        return f'{rounded.normalize():f}'

    if field == 'Effect':
        return EFFECT_LABELS.get(text, text)
    if field == 'ImplicitTarget_0':
        return TARGET_LABELS.get(text, text)
    if field == 'EffectAura':
        return f'{text}（{AURA_LABELS[text]}）' if text in AURA_LABELS else text
    if field in ('RecoveryTime', 'CategoryRecoveryTime'):
        try:
            return f'{(Decimal(text) / Decimal(1000)).normalize():f} 秒'
        except (InvalidOperation, ValueError, TypeError):
            return text
    if field == 'PvpMultiplier':
        try:
            return f'{readable_number(Decimal(text) * Decimal(100))}%'
        except (InvalidOperation, ValueError, TypeError):
            return text
    if field in ('EffectBonusCoefficient', 'BonusCoefficientFromAP'):
        try:
            return f'{readable_number(Decimal(text) * Decimal(100))}%'
        except (InvalidOperation, ValueError, TypeError):
            return text
    if field in ('EffectBasePointsF', 'EffectBasePoints'):
        try:
            number = Decimal(text)
            return readable_number(number, places='0.0001' if abs(number) < Decimal('0.01') else '0.01')
        except (InvalidOperation, ValueError, TypeError):
            return text
    if field == 'SchoolMask' and text == '1':
        return '物理'
    return text


def display_hotfix_value(field, value):
    """Translate a known DB2 field enum; unknown values remain exact."""
    return _value(field, value)


def project_hotfix_columns(table, after, baseline=None, *, max_fields=6):
    """Return useful per-field facts, with client-base comparison kept distinct."""
    key = str(table or '').lower()
    fields = FIELDS.get(key) or []
    if not isinstance(after, dict):
        return []
    result = []
    if key == 'spellmisc':
        permissions = spell_misc_permission_deltas(after, baseline)
        if permissions:
            result.append({'field': 'Attributes',
                           'label': '施放条件标志 / Attributes_*',
                           'text': permissions, 'base_changed': True})
    for field, label in fields:
        raw = after.get(field)
        if raw is None or str(raw).strip() == '':
            continue
        old = baseline.get(field) if isinstance(baseline, dict) else None
        comparable = old is not None and str(old).strip() != ''
        changed = comparable and not _same(old, raw)
        if comparable and not changed and field not in ('Effect', 'ImplicitTarget_0'):
            continue
        if not changed and str(raw).strip() in ('0', '0.0') and field not in (
                'RecoveryTime', 'RangeIndex', 'CastingTimeIndex'):
            continue
        if field == 'PvpMultiplier' and not changed and _same(raw, '1'):
            continue
        value = _value(field, raw)
        old_text = _value(field, old) if changed else ''
        result.append({
            'field': field,
            'label': f'{label} / {field}',
            'text': f'{old_text} → {value}' if changed else value,
            'base_changed': bool(changed),
        })
        if len(result) >= max_fields:
            break
    return result
