"""原生状态的施加来源；只限定专精归属，不参与全局/局部增伤分类。"""

# sc_warrior.cpp 会为三个专精预建这些目标对象，构造对象本身不证明可施加。
# 以下绑定交叉核对 DBC 天赋 spec_ids 与原生 trigger() 调用所属技能/英雄树。
NATIVE_STATE_OWNERS = {
    210824: {'class': 'mage', 'specs': ('arcane',), 'talent_spell_ids': (321507,),
             'native_source': 'touch_of_the_magi_t -> 321507 effect 1 -> 210824'},
    208086: {'class': 'warrior', 'specs': ('arms',), 'talent_spell_ids': (167105,),
             'native_source': 'colossus_smash_t::impact -> debuffs_colossus_smash'},
    445836: {'class': 'warrior', 'specs': ('arms', 'fury'), 'talent_spell_ids': (444772,),
             'native_source': 'talents.slayer.overwhelming_blades -> debuffs_overwhelmed'},
    447513: {'class': 'warrior', 'specs': ('arms', 'protection'), 'talent_spell_ids': (429636,),
             'native_source': 'demolish_t -> debuffs_wrecked; talents.colossus'},
    1299405: {'class': 'warrior', 'specs': ('arms', 'protection'), 'talent_spell_ids': (228920,),
              'native_source': 'ravager_tick_t -> debuffs_ravaged; Ravager effect 3 trigger'},
}


def global_effect_matches_owner(effect, class_name, spec, talent_scopes=None):
    """先用施加来源限定范围，再校验展示目录；共享对象不能扩大天赋归属。"""
    if effect.get('source_class') and effect['source_class'] != class_name:
        return False
    for spell_id in effect.get('source_spell_ids') or []:
        owner = NATIVE_STATE_OWNERS.get(spell_id)
        if owner and (class_name != owner['class'] or spec not in owner['specs']):
            return False
        if talent_scopes and spell_id in talent_scopes:
            scopes = talent_scopes[spell_id]
            if scopes and spec not in scopes:
                return False
    return not effect.get('specializations') or spec in effect['specializations']
