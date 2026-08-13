"""Authoritative Chinese labels for SimC consumable option values."""

SIMC_CONSUMABLE_LABELS = {
    # Midnight flasks
    'blood_knights_2': '血骑士合剂',
    'flask_of_thalassian_resistance_2': '萨拉斯抗性合剂',
    'flask_of_the_blood_knights_2': '血骑士合剂',
    'flask_of_the_magisters_2': '魔导师合剂',
    'flask_of_the_shattered_sun_2': '破碎残阳合剂',
    'magisters_2': '魔导师合剂',
    # Midnight potions
    'draught_of_rampant_abandon_2': '狂放恣意饮剂',
    'lights_potential_2': '圣光潜力',
    'potion_of_recklessness_2': '鲁莽药水',
    # Midnight food
    'blooming_feast': '盛放筵席',
    'harandar_celebration': '哈籁恩达尔庆典大餐',
    'royal_roast': '皇家烤肉',
    'silvermoon_parade': '银月城浮华大餐',
    # Midnight augment runes and weapon oils
    'void_touched': '虚触强化符文',
    'void_touched_augment_rune': '虚触强化符文',
    'thalassian_phoenix_oil_2': '萨拉斯凤凰之油',
    # Historical tokens retained for older upstream Profiles.
    'flask_of_alchemical_chaos_3': '炼金混沌合剂',
    'tempered_flask': '淬火合剂',
    'tempered_potion': '淬火药水',
    'tempered_potion_3': '淬火药水',
    'feast_of_the_midnight': '午夜舞会盛宴',
    'the_sushi_special': '特色寿司',
    'crystallized': '晶化强化符文',
    'draconic_augmentation': '龙族强化符文',
    'algari_mana_oil': '阿加法力之油',
    'algari_mana_oil_3': '阿加法力之油',
}

SIMC_CONSUMABLE_CONDITION_LABELS = {
    '!(talent.rite_of_adjuration.enabled|talent.rite_of_sanctification.enabled)':
        '未选择祈告仪式或圣化仪式天赋时',
    '!talent.flametongue_weapon': '未选择火舌武器天赋时',
}


def simc_consumable_option(value):
    """Return a localized display row while preserving the exact SimC value."""
    normalized = str(value or '').strip()
    base_token, separator, condition = normalized.partition(',if=')
    label = SIMC_CONSUMABLE_LABELS.get(base_token, base_token)
    if separator:
        condition_label = SIMC_CONSUMABLE_CONDITION_LABELS.get(condition)
        if condition_label:
            label = f'{label}（{condition_label}）'
    return {'value': normalized, 'label': label}
