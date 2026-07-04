# -*- coding: utf-8 -*-
"""魔兽世界英雄天赋树中文名和专精可选关系。"""

HERO_SUBTREE_NAME_ZH = {
    'Aldrachi Reaver': '奥达奇掠夺者',
    'Archon': '执政官',
    'Chronowarden': '时空守卫',
    'Colossus': '巨像',
    'Conduit of the Celestials': '天神御师',
    'Dark Ranger': '黑暗游侠',
    'Deathbringer': '死亡使者',
    'Deathstalker': '死亡猎手',
    'Diabolist': '恶魔使徒',
    'Druid of the Claw': '利爪德鲁伊',
    "Elune's Chosen": '艾露恩钦选者',
    'Farseer': '先知',
    'Fatebound': '命缚者',
    'Fel-Scarred': '邪痕者',
    'Flameshaper': '塑焰者',
    'Frostfire': '霜火',
    'Hellcaller': '地狱召唤者',
    'Herald of the Sun': '太阳使者',
    'Keeper of the Grove': '丛林守护者',
    'Lightsmith': '圣光匠',
    'Master of Harmony': '祥和宗师',
    'Mountain Thane': '山丘领主',
    'Oracle': '神谕者',
    'Pack Leader': '兽群领袖',
    "Rider of the Apocalypse": '天启骑士',
    "San'layn": '萨莱因',
    'Scalecommander': '鳞长',
    'Sentinel': '哨兵',
    'Shado-Pan': '影踪派',
    'Slayer': '屠戮者',
    'Soul Harvester': '灵魂收割者',
    'Spellslinger': '法术投射者',
    'Stormbringer': '风暴使者',
    'Sunfury': '日怒',
    'Templar': '圣殿骑士',
    'Totemic': '图腾祭司',
    'Trickster': '欺诈者',
    'Voidweaver': '虚空编织者',
    'Wildstalker': '荒野追猎者',
}

# 当前 DB2 TraitSubTree.csv 中 18-66 为 The War Within 英雄天赋树。
HERO_SUBTREE_ID_TO_NAME = {
    18: 'Voidweaver',
    19: 'Archon',
    20: 'Oracle',
    21: 'Druid of the Claw',
    22: 'Wildstalker',
    23: 'Keeper of the Grove',
    24: "Elune's Chosen",
    31: "San'layn",
    32: 'Rider of the Apocalypse',
    33: 'Deathbringer',
    34: 'Fel-Scarred',
    35: 'Aldrachi Reaver',
    36: 'Scalecommander',
    37: 'Flameshaper',
    38: 'Chronowarden',
    39: 'Sunfury',
    40: 'Spellslinger',
    41: 'Frostfire',
    42: 'Sentinel',
    43: 'Pack Leader',
    44: 'Dark Ranger',
    48: 'Templar',
    49: 'Lightsmith',
    50: 'Herald of the Sun',
    51: 'Trickster',
    52: 'Fatebound',
    53: 'Deathstalker',
    54: 'Totemic',
    55: 'Stormbringer',
    56: 'Farseer',
    57: 'Soul Harvester',
    58: 'Hellcaller',
    59: 'Diabolist',
    60: 'Slayer',
    61: 'Mountain Thane',
    62: 'Colossus',
    64: 'Conduit of the Celestials',
    65: 'Shado-Pan',
    66: 'Master of Harmony',
}

# 游戏内每个专精只能在两棵英雄天赋树中二选一；这里存英文 canonical 名称，
# 运行时再根据当前 DB 的 subtree ID/名称过滤。
SPEC_HERO_SUBTREE_NAMES = {
    ('DeathKnight', 'Blood'): ("San'layn", 'Deathbringer'),
    ('DeathKnight', 'Frost'): ('Deathbringer', 'Rider of the Apocalypse'),
    ('DeathKnight', 'Unholy'): ('Rider of the Apocalypse', "San'layn"),
    ('DemonHunter', 'Havoc'): ('Aldrachi Reaver', 'Fel-Scarred'),
    ('DemonHunter', 'Vengeance'): ('Aldrachi Reaver', 'Fel-Scarred'),
    ('Druid', 'Balance'): ("Elune's Chosen", 'Keeper of the Grove'),
    ('Druid', 'Feral'): ('Druid of the Claw', 'Wildstalker'),
    ('Druid', 'Guardian'): ('Druid of the Claw', "Elune's Chosen"),
    ('Druid', 'Restoration'): ('Keeper of the Grove', 'Wildstalker'),
    ('Evoker', 'Augmentation'): ('Scalecommander', 'Chronowarden'),
    ('Evoker', 'Devastation'): ('Scalecommander', 'Flameshaper'),
    ('Evoker', 'Preservation'): ('Chronowarden', 'Flameshaper'),
    ('Hunter', 'BeastMastery'): ('Pack Leader', 'Dark Ranger'),
    ('Hunter', 'Marksmanship'): ('Dark Ranger', 'Sentinel'),
    ('Hunter', 'Survival'): ('Pack Leader', 'Sentinel'),
    ('Mage', 'Arcane'): ('Sunfury', 'Spellslinger'),
    ('Mage', 'Fire'): ('Sunfury', 'Frostfire'),
    ('Mage', 'Frost'): ('Frostfire', 'Spellslinger'),
    ('Monk', 'Brewmaster'): ('Master of Harmony', 'Shado-Pan'),
    ('Monk', 'Mistweaver'): ('Conduit of the Celestials', 'Master of Harmony'),
    ('Monk', 'Windwalker'): ('Conduit of the Celestials', 'Shado-Pan'),
    ('Paladin', 'Holy'): ('Herald of the Sun', 'Lightsmith'),
    ('Paladin', 'Protection'): ('Lightsmith', 'Templar'),
    ('Paladin', 'Retribution'): ('Herald of the Sun', 'Templar'),
    ('Priest', 'Discipline'): ('Oracle', 'Voidweaver'),
    ('Priest', 'Holy'): ('Archon', 'Oracle'),
    ('Priest', 'Shadow'): ('Archon', 'Voidweaver'),
    ('Rogue', 'Assassination'): ('Deathstalker', 'Fatebound'),
    ('Rogue', 'Outlaw'): ('Fatebound', 'Trickster'),
    ('Rogue', 'Subtlety'): ('Deathstalker', 'Trickster'),
    ('Shaman', 'Elemental'): ('Farseer', 'Stormbringer'),
    ('Shaman', 'Enhancement'): ('Stormbringer', 'Totemic'),
    ('Shaman', 'Restoration'): ('Farseer', 'Totemic'),
    ('Warlock', 'Affliction'): ('Soul Harvester', 'Hellcaller'),
    ('Warlock', 'Demonology'): ('Soul Harvester', 'Diabolist'),
    ('Warlock', 'Destruction'): ('Hellcaller', 'Diabolist'),
    ('Warrior', 'Arms'): ('Slayer', 'Colossus'),
    ('Warrior', 'Fury'): ('Slayer', 'Mountain Thane'),
    ('Warrior', 'Protection'): ('Mountain Thane', 'Colossus'),
}


def hero_subtree_name_zh(name):
    """返回英雄天赋树中文名；未收录时返回原名。"""
    if not name:
        return name
    return HERO_SUBTREE_NAME_ZH.get(str(name).strip(), name)


def hero_subtree_name_by_id(subtree_id):
    try:
        return HERO_SUBTREE_ID_TO_NAME.get(int(subtree_id or 0), '')
    except (TypeError, ValueError):
        return ''


def spec_hero_subtree_names(class_name, spec_name):
    return SPEC_HERO_SUBTREE_NAMES.get((class_name or '', spec_name or ''), ())
