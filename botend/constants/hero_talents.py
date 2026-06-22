# -*- coding: utf-8 -*-
"""魔兽世界英雄天赋树中文名映射。"""

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


def hero_subtree_name_zh(name):
    """返回英雄天赋树中文名；未收录时返回原名。"""
    if not name:
        return name
    return HERO_SUBTREE_NAME_ZH.get(str(name).strip(), name)
