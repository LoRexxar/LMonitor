"""原生专精字段的已核对语义；类型不能根据字段名猜测。

每组注释给出 SimC 实现文件。此处只说明引擎判断，不替 APL 编写战术理由。
"""

VALUES = {
    # engine/player/player.cpp、engine/sim/sim.cpp
    'enemies': ('配置的敌人总数', '个'),
    'time_to_bloodlust': ('预计距离嗜血的时间', '秒'),
    'self.target': ('角色当前主目标的编号', ''), 'target': ('当前检查目标的编号', ''),
    # sc_death_knight.cpp:create_expression
    'death_knight.first_ams_cast': ('配置的首次反魔法护罩施放时间', '秒'),
    # sc_druid.cpp:create_expression；保留配置数值，不猜测其单位。
    'druid.time_spend_healing': ('配置的治疗时间参数', ''),
    # sc_evoker.cpp:create_expression；allied_cds_up 是数量，不是冷却是否就绪。
    'evoker.allied_cds_up': ('受黑檀之力影响的队友中处于主要爆发的数量（无黑檀目标时检查先知先觉目标）', '个'),
    'evoker.shifting_buffs': ('受自己的流沙影响的队友数量', '个'),
    # sc_mage.cpp:create_expression
    'hot_streak_spells_in_flight': ('能够触发炽热连击且尚未命中的法术数量', '个'),
    'icicles': ('当前冰刺数量', '个'),
    # paladin/sc_paladin.cpp:create_expression
    'next_armament': ('下一次召唤的武装类型', ''),
    'holy_bulwark': ('神圣壁垒', ''), 'sacred_weapon': ('神圣武器', ''),
    # sc_rogue.cpp:create_expression、create_resource_expression
    'cp_max_spend': ('终结技最多可消耗的连击点', '点'),
    'rtb_buffs': ('命运骨骰当前效果值', ''),
    'energy.base_deficit': ('不计临时上限时的能量缺口', '点'),
    'energy.regen_combined': ('包含额外回复来源的每秒能量回复', '点'),
    'energy.base_time_to_max': ('不计临时上限时能量回满所需时间', '秒'),
    # sc_shaman.cpp:lightning_rod 的目标增益回调维护计数。
    'lightning_rod': ('带有闪电之杖效果的目标数', '个'),
}

PREDICATES = {
    # monk/sc_monk.cpp:is_combo_strike。首次且可参与连击的技能也返回真。
    'combo_strike': ('本技能可参与连击，且与上次计入连击的技能不同或尚无上次记录',
                     '本技能不能参与连击，或与上次计入连击的技能相同'),
    # sc_hunter.cpp:create_expression
    'max_prio_damage': ('配置启用了优先目标伤害模式', '配置未启用优先目标伤害模式'),
    'howl_summon.ready': ('兽群领袖的飞龙、野猪或熊召唤增益至少有一个就绪',
                          '兽群领袖的飞龙、野猪和熊召唤增益均未就绪'),
    # sc_mage.cpp:create_action_expression。门槛取自天赋，不写死百分比。
    'scorch_execute.active': ('已学习灼烧且目标处于其斩杀血量范围', '未学习灼烧或目标不在其斩杀血量范围'),
    'firestarter.active': ('已学习纵火者且目标处于其生效血量范围', '未学习纵火者或目标不在其生效血量范围'),
    # sc_death_knight.cpp:create_expression
    'drw.bp_ticking': ('符文刃舞武器仍在场且其血之疫病正在当前目标上生效', '符文刃舞武器不在场或其血之疫病未在当前目标上生效'),
    'death_and_decay.ticking': ('存在正在生效的枯萎凋零或亵渎区域', '没有正在生效的枯萎凋零或亵渎区域'),
    # sc_druid.cpp:create_expression。eclipse 是月蚀覆盖状态，不能当成日月蚀增益的 up。
    'druid.no_cds': ('配置禁用了爆发技能', '配置未禁用爆发技能'),
    'eclipse.lunar': ('日月蚀被切换为月蚀模式', '日月蚀未被切换为月蚀模式'),
    'eclipse.solar': ('日月蚀处于未被月蚀覆盖的日蚀模式', '日月蚀已被月蚀覆盖'),
    # paladin/sc_paladin.cpp:create_consecration_expression
    'consecration.up': ('存在正在生效的奉献区域', '没有正在生效的奉献区域'),
    # priest/sc_priest.cpp:create_expression
    'priest.force_devour_matter': ('配置强制启用了吞噬物质行为', '配置未强制启用吞噬物质行为'),
    # sc_rogue.cpp:create_expression
    'stealthed.rogue': ('潜行、消失、暗影之舞或诡诈提供的潜行状态正在生效',
                        '潜行、消失、暗影之舞和诡诈提供的潜行状态均未生效'),
    'priority_rotation': ('配置启用了优先目标循环', '配置未启用优先目标循环'),
    # sc_shaman.cpp:create_expression
    'ti_lightning_bolt': ('当前无索林姆祈咒记录，或记录的是闪电箭／风暴',
                          '索林姆祈咒记录的不是闪电箭或风暴'),
    'ti_chain_lightning': ('索林姆祈咒记录的是闪电链', '索林姆祈咒记录的不是闪电链'),
    # warlock/sc_warlock.cpp:create_expression
    'demonic_art': ('恶魔之艺的霸主、主母或深渊领主效果至少有一个生效',
                    '恶魔之艺的霸主、主母和深渊领主效果均未生效'),
}
