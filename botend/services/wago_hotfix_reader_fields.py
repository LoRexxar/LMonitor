"""Reader-facing Hotfix columns; DB2 client rows are context, not live predecessors."""
from decimal import Decimal, InvalidOperation


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
    '10': '治疗', '189': '拾取',
}
TARGET_LABELS = {
    '1': '施法者', '6': '敌方目标', '18': '施法者位置',
    '21': '友方目标', '25': '任意目标',
}
AURA_LABELS = {
    '218': '标签百分比修正', '430': '播放场景',
    '649': '标签 PvP 倍率百分比修正',
}


def _same(left, right):
    if str(left) == str(right):
        return True
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError, TypeError):
        return False


def _value(field, raw):
    text = str(raw).strip()
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
            return f'{(Decimal(text) * Decimal(100)).normalize():f}%'
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
