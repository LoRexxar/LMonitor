"""将正式战斗日志中的消耗品证据归属到对应战斗和角色。"""
from collections import Counter, defaultdict


# 食物按可辨认的饱餐效果归并。多种料理可能授予同一个法术，不能反推菜名。
FOOD = {
    (451921,): '饱餐（耐力）',
    (462209,): '饱餐（全能与精通）',
    (1294727, 1232585, 1285644): '筵席（主属性与耐力）',
    (1232324, 1233724): '饱餐（50 主属性）',
    (1233407, 1233732): '饱餐（35 主属性）',
    (1232325, 1233714): '饱餐（25 主属性）',
    (451918, 1232490, 1219183, 1284644, 1284616, 1233703): '饱餐（暴击）',
    (451916, 1232493, 1232500, 1219182, 1284643, 1284617, 1233704): '饱餐（急速）',
    (451920, 1232491, 1219185, 1284642, 1284618, 1233705): '饱餐（精通）',
    (451917, 1219184, 1232501, 1232492, 1284641, 1284619, 1233706, 1283372, 1284650): '饱餐（全能）',
    (1232086, 1232076): '筵席（暴击与耐力）',
    (1232087, 1232078): '筵席（急速与耐力）',
    (1232089, 1232080): '筵席（精通与耐力）',
    (1232091, 1232082): '筵席（全能与耐力）',
    (1232316, 1233708): '远行者口粮',
    (1232318, 1233710): '采集者什锦餐',
    (1232313, 1233707): '便携小食',
    (1232317, 1233709): '速食三明治',
    (1232320, 1233711): '银月城标准餐',
    (1232321, 1233712): '香料饼干',
    (1233400, 1233726): '血蓟肉排',
    (1233404, 1233729): '永歌布丁',
    (1233406, 1233731): '油炸花尾鱼',
    (1233401, 1233727): '炉火晚餐',
    (1233405, 1233730): '太阳之井佳肴',
    (1233402, 1233719): '智者之尾',
}
BUFFS = {
    **{spell: ('food', name) for spells, name in FOOD.items() for spell in spells},
    1235057: ('flask', '萨拉斯抗性合剂'),
    1235108: ('flask', '魔导师合剂'),
    1235110: ('flask', '血骑士合剂'),
    1235111: ('flask', '破碎残阳合剂'),
}
POTIONS = {
    1234768: ('healing', '银月城治疗药水'),
    1295247: ('healing', '浓缩银月城治疗药水'),
    1262857: ('healing', '强效治疗药水'),
    1236994: ('potion', '鲁莽药水'),
    1236998: ('potion', '狂放恣意饮剂'),
    1238443: ('potion', '狂热药水'),
    1239479: ('potion', '噬梦药水'),
    1295015: ('potion', '诱惑秘药'),
    1236648: ('potion', '光融法力药水'),
    1235568: ('potion', '圣光护持'),
    1236590: ('potion', '焕新血清'),
    1236616: ('potion', '圣光潜力'),
    1263074: ('potion', '阿曼尼提取物'),
    1295132: ('potion', '液态光泽'),
}
# 武器油和磨刀石使用装备上的临时附魔编号，不能与永久附魔混用。
WEAPON = {
    7905: '耀辉磨刀石', 7906: '耀辉磨刀石',
    7907: '耀辉平衡石', 7908: '耀辉平衡石',
    8051: '萨拉斯凤凰之油', 8052: '萨拉斯凤凰之油',
    8053: '黎明之油', 8054: '黎明之油',
    8055: '走私者的附魔锋刃', 8056: '走私者的附魔锋刃',
}
CATEGORIES = {'food': '食物', 'flask': '合剂', 'potion': '战斗与功能药水', 'healing': '治疗药水',
              'weapon': '武器临时强化'}
SPELLS = {**BUFFS, **POTIONS}
EVENT_FILTER = (
    f"(type in ('applybuff', 'refreshbuff') and ability.id in ({','.join(map(str, BUFFS))})) or "
    f"(type = 'cast' and ability.id in ({','.join(map(str, POTIONS))}))"
)


def spell_id(event):
    ability = event.get('abilityGameID') or event.get('ability')
    if isinstance(ability, dict):
        ability = ability.get('guid') or ability.get('id')
    return ability


EVENT_SELECTION = """
    fights(fightIDs:$fightIDs) { id startTime endTime }
    consumableEvents: events(fightIDs:$fightIDs, dataType:All, filterExpression:$filter,
                             startTime:$useStart, limit:10000) { data nextPageTimestamp }
"""


def fetch_usage(client, code, fights, combatants, first_report):
    """按整批获取过滤后的事件。分页未完成时禁止写入部分结果。"""
    if not combatants:
        return combatants
    query = ("query($code:String!, $fightIDs:[Int], $filter:String!, $useStart:Float) {"
             " reportData { report(code:$code) {" + EVENT_SELECTION + "} } }")
    observed = defaultdict(lambda: defaultdict(set))
    report = first_report
    cursor = None
    for page in range(20):
        if page:
            variables = dict(code=code, fightIDs=fights, filter=EVENT_FILTER, useStart=cursor)
            data = client._wcl_graphql(query, variables, retries=2)
            if not data:
                client._wcl_last_error = client._wcl_last_error or 'request_failed'
                return None
            report = (data.get('reportData') or {}).get('report') or {}
        envelope = report.get('consumableEvents') or {}
        if not isinstance(envelope.get('data'), list) or 'nextPageTimestamp' not in envelope:
            client._wcl_last_error = 'invalid_payload'
            return None
        windows = report.get('fights') or []
        for event in envelope['data']:
            if not isinstance(event, dict):
                client._wcl_last_error = 'invalid_payload'
                return None
            kind = event.get('type')
            spell = spell_id(event)
            if kind == 'cast' and spell in POTIONS:
                actor = event.get('sourceID')
            elif kind in ('applybuff', 'refreshbuff') and spell in BUFFS:
                actor = event.get('targetID')
            else:
                continue
            fight = event.get('fight')
            if fight is None:
                timestamp = event.get('timestamp')
                matches = fights if len(fights) == 1 else [
                    row['id'] for row in windows if isinstance(timestamp, (int, float))
                    and row['startTime'] <= timestamp <= row['endTime']]
                fight = matches[0] if len(matches) == 1 else None
            if fight not in fights or not isinstance(actor, int):
                client._wcl_last_error = 'invalid_payload'
                return None
            category = SPELLS[spell][0]
            observed[fight, actor][category].add(spell)
        next_cursor = envelope['nextPageTimestamp']
        if next_cursor is None:
            break
        if type(next_cursor) not in (int, float) or (cursor is not None and next_cursor <= cursor):
            client._wcl_last_error = 'invalid_pagination'
            return None
        cursor = next_cursor
    else:
        client._wcl_last_error = 'pagination_limit'
        return None
    for row in combatants:
        uses = observed[row.get('fight'), row.get('sourceID')]
        auras = row.get('auras')
        if isinstance(auras, list):
            for aura in auras:
                spell = spell_id(aura) if isinstance(aura, dict) else None
                if spell in BUFFS:
                    uses[BUFFS[spell][0]].add(spell)
        # 缺少开场增益清单时，食物与合剂没有完整的观测窗口。
        categories = ('food', 'flask', 'potion', 'healing') if isinstance(auras, list) else ('potion', 'healing')
        row['consumables'] = {key: sorted(uses[key]) for key in categories}
    return combatants


def statistics(records):
    counts = defaultdict(Counter)
    coverage = Counter()
    seen = set()
    for row in records:
        usage = row.get('consumables')
        if not usage:
            continue
        actor = row.get('wcl_actor_id')
        if actor:
            identity = (row['report_code'], row['fight_id'], actor)
            if identity in seen:
                continue
            seen.add(identity)
        for category, ids in usage.items():
            if category not in CATEGORIES or not isinstance(ids, list):
                continue
            coverage[category] += 1
            if category == 'weapon':
                names = {WEAPON[i] for i in ids if i in WEAPON}
            else:
                names = {SPELLS[i][1] for i in ids if i in SPELLS and SPELLS[i][0] == category}
            counts[category].update(names)
    return [dict(key=key, label=label, samples=coverage[key],
                 rows=[dict(name=name, count=count, pct=round(100 * count / coverage[key], 1))
                       for name, count in sorted(counts[key].items(), key=lambda pair: (-pair[1], pair[0]))])
            for key, label in CATEGORIES.items()]
