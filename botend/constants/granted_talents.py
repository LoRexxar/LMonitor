# -*- coding: utf-8 -*-
"""职业树赠送天赋与专精的映射关系

每个专精在进入职业树时会自动获得一个或多个起手技能（赠送天赋）。
这些天赋在 DB2 数据中通过 flags=8 AND parents=[] 识别，但无法区分它们分别属于哪个专精。
因此需要手动维护这个映射表。

映射格式：
GRANTED_TALENTS_BY_SPEC = {
    ('ClassName', 'SpecName'): [spell_id1, spell_id2, ...],
}
"""

GRANTED_TALENTS_BY_SPEC = {
    # 死亡骑士
    ('DeathKnight', 'Blood'): [49998],   # 灵界打击
    ('DeathKnight', 'Frost'): [48792],   # 冰封之韧
    ('DeathKnight', 'Unholy'): [46585],  # 亡者复生
    
    # 战士
    ('Warrior', 'Arms'): [386164],       # 战斗姿态
    ('Warrior', 'Fury'): [386196],       # 狂暴姿态
    ('Warrior', 'Protection'): [386208], # 防御姿态
    
    # 恶魔猎手
    ('DemonHunter', 'Havoc'): [198793],      # 复仇回避 (Vengeful Retreat)
    ('DemonHunter', 'Vengeance'): [207684],  # 悲苦咒符 (Sigil of Misery)
    
    # 德鲁伊
    ('Druid', 'Balance'): [197628],    # 星火术 (Starfire) - 平衡
    ('Druid', 'Feral'): [1822],        # 斜掠 (Rake) - 野性
    ('Druid', 'Guardian'): [22842],    # 狂暴回复 (Frenzied Regeneration) - 守护
    ('Druid', 'Restoration'): [774],   # 回春术 (Rejuvenation) - 恢复
    
    # 猎人
    ('Hunter', 'BeastMastery'): [109215],  # 迅疾如风
    ('Hunter', 'Marksmanship'): [264735],  # 优胜劣汰
    ('Hunter', 'Survival'): [385539],      # 春回大地
    
    # 牧师
    ('Priest', 'Discipline'): [14914],   # 神圣之火 (Holy Fire) - 戒律
    ('Priest', 'Holy'): [393870],        # 强化快速治疗 - 神圣
    ('Priest', 'Shadow'): [8092],        # 心灵震爆 (Mind Blast) - 暗影
    
    # 潜行者
    ('Rogue', 'Assassination'): [5938],  # 毒刃
    ('Rogue', 'Outlaw'): [2094],         # 致盲
    ('Rogue', 'Subtlety'): [31224],      # 暗影斗篷
    
    # 萨满
    ('Shaman', 'Elemental'): [51505],    # 熔岩爆裂
    ('Shaman', 'Enhancement'): [60103],  # 熔岩猛击
    ('Shaman', 'Restoration'): [1064],   # 治疗链
}


def get_granted_talent_spell_ids(class_name, spec_name):
    """获取指定专精的赠送天赋 spell_id 列表"""
    return GRANTED_TALENTS_BY_SPEC.get((class_name, spec_name), [])


def is_granted_talent_for_spec(spell_id, class_name, spec_name):
    """判断指定 spell_id 是否为该专精的赠送天赋"""
    granted_ids = get_granted_talent_spell_ids(class_name, spec_name)
    return spell_id in granted_ids
