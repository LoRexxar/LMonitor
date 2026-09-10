"""复核 DBC 选择器的含义，区分公共类别和具体技能；不读取描述进行分类。"""
import ast
from collections import defaultdict
import hashlib
import json
import re
from pathlib import Path

# 已在固定版本的 DBC 完整集合及原生注册中复核的公共伤害选择位。
# 匹配的是作用域选择器，任何天赋或状态使用同一选择器都会得到同一结论。
COMMON_SELECTORS = {
    3: (0, 8, '法师通用伤害技能'),
    4: (0, 512, '战士通用伤害技能'),
    5: (1, 131072, '术士通用伤害技能'),
    6: (2, 256, '牧师通用伤害技能'),
    9: (3, 1024, '猎人通用伤害技能'),
    11: (1, 128, '萨满通用伤害法术'),
    15: (1, 2048, '死亡骑士通用伤害技能'),
    53: (3, 128, '武僧通用伤害技能'),
    107: (0, 65536, '恶魔猎手通用伤害技能'),
    224: (0, 1, '唤魔师通用伤害技能'),
}
REVIEWED_REVISION = 'ba72a9dcfe88e6e80384f52f7786619c5dfcd6f1'
LOCAL_SELECTORS = {
    (224, (4096, 0, 0, 0)): {
        'ids': {362969, 1265872}, 'label': '碧蓝打击与碧蓝横扫',
        'file': 'engine/class_modules/sc_evoker.cpp',
        'markers': ['struct azure_strike_t : public azure_strike_base_t',
                    'struct azure_sweep_t : public azure_strike_base_t',
                    'talent.azure_sweep_spell            = find_spell( 1265872 )'],
    },
    (4, (128, 0, 0, 0)): {
        'ids': {6343, 435222}, 'label': '雷霆一击与雷霆轰击',
        'file': 'engine/class_modules/sc_warrior.cpp',
        'markers': ['struct thunder_blast_t : public warrior_attack_t',
                    'struct thunder_clap_t : public warrior_attack_t',
                    'p->find_spell( 435222 )'],
    },
}
CATEGORY_AURAS = {
    79: '伤害学校', 163: '暴击伤害', 168: '对指定生物类型的伤害',
    276: '指定机制的伤害（包括流血）', 303: '对指定光环状态目标的伤害',
    319: '自动攻击速度', 344: '自动攻击伤害', 429: '宠物伤害',
    530: '自动攻击伤害', 531: '守护者伤害',
    270: '受到本角色的学校伤害', 343: '受到本角色的自动攻击伤害',
    380: '受到本角色守护者的伤害', 381: '受到本角色宠物的伤害',
}

# 同版 DBC 用作类别的选择位；必须先于一般的部分技能集合判定。
# 不匹配来源天赋名称，使用此位的所有效果按同一范围处理。
CATEGORY_SELECTORS = {
    4: [(3, 16777216, '流血伤害')],
    7: [(0, 262144, '施法形态伤害'), (2, 8192, '熊形态伤害'), (3, 4, '猎豹形态伤害')],
    8: [(3, 134217728, '暗影伤害')],
    9: [(0, 1, '自动射击及其替代分量')],
    11: [(3, 1073741824, '自然伤害')],
    15: [(3, 16777216, '冰霜伤害')],
    107: [(2, 2147483648, '吞噬专精通用伤害')],
}


def aggregate(line):
    value = line[line.index('{'):line.rindex('}') + 1]
    value = re.sub(r'"(?:\\.|[^"\\])*"|[{}]',
                   lambda m: m[0] if m[0].startswith('"') else '[' if m[0] == '{' else ']', value)
    return ast.literal_eval(value)


def load_dbc(source):
    """字段位置取自同版 C++ 结构声明，拒绝静默套用其他版本布局。"""
    header = (source / 'engine/dbc/spell_data.hpp').read_text(encoding='utf-8')
    layouts = {}
    for struct, end in [('spell_data_t', 'unsigned equipped_class()'), ('spelleffect_data_t', 'bool ok()')]:
        body = header.split('struct ' + struct + '\n{', 1)[1].split(end, 1)[0]
        body = re.sub(r'//[^\n]*', '', body)
        layouts[struct] = re.findall(r'\b(_\w+)\s*(?:\[[^;]*?\])?\s*;', body)
    spells, effects, stage = {}, {}, None
    for line in (source / 'engine/dbc/generated/sc_spell_data.inc').read_text(encoding='utf-8').splitlines():
        if line.startswith('static spell_data_t '): stage = 'spell_data_t'
        elif line.startswith('static spelleffect_data_t '): stage = 'spelleffect_data_t'
        elif line.startswith('};'): stage = None
        if not stage or not line.startswith('  {'): continue
        values = aggregate(line)
        if len(values) != len(layouts[stage]):
            raise ValueError('DBC 聚合数据与同版 C++ 声明不匹配')
        row = dict(zip(layouts[stage], values))
        (spells if stage == 'spell_data_t' else effects)[row['_id']] = row
    return spells, effects


class ScopeResolver:
    def own_damage_relations(self, sid):
        """同源法术的伤害本体也属于状态的局部分量，不能随公共修正一起清除。"""
        result=[]
        for e in self.by_spell.get(sid,[]):
            if not (e['_type'] in (2,9,31,58,121) or (e['_type']==6 and e['_subtype'] in (3,53,89))):continue
            result.append({'effect_id':e['_id'],'source_spell_id':sid,'effect_index':e['_index']+1,
                'class_family':self.spells[sid]['_class_flags_family'],'type':e['_type'],'subtype':e['_subtype'],
                'misc1':e['_misc_value'],'misc2':e['_misc_value_2'],'base_value':e['_base_value'],
                'flags':e['_class_flags'],'method':'no_static_spell_selector','affected_spells':[]})
        return result

    def __init__(self, source, revision, catalog):
        self.source, self.revision = source, revision
        # 复核记录针对选择器的完整范围，不针对某个天赋名称。
        self.skill_sets = json.loads((Path(__file__).with_name('simc_scope_skill_sets.json')).read_text(encoding='utf-8'))
        self.spells, self.effects = load_dbc(source)
        self.by_spell = defaultdict(list)
        for e in self.effects.values(): self.by_spell[e['_spell_id']].append(e)
        self.damage = {sid for sid, effects in self.by_spell.items() if any(
            e['_type'] in (2, 9, 31, 58, 121) or (e['_type'] == 6 and e['_subtype'] in (3, 53, 89)) for e in effects)}
        self.damage_families = defaultdict(set)
        for sid in self.damage:
            self.damage_families[self.spells[sid]['_class_flags_family']].add(sid)
        roots = set()
        for file in ('class_spells.inc', 'specialization_spells.inc'):
            for line in (source / 'engine/dbc/generated' / file).read_text(encoding='utf-8').splitlines():
                if line.startswith('  {'):
                    row = aggregate(line)
                    if row[0]: roots.add(row[2])
        roots.update(r['spell_id'] for r in catalog['talent_catalog'] if r['spell_id'] in self.damage)
        self.roots = roots
        self.flag_members = defaultdict(set)
        for sid, spell in self.spells.items():
            for word, mask in enumerate(spell['_class_flags']):
                for bit in range(32):
                    if mask & (1 << bit): self.flag_members[(spell['_class_flags_family'], word, 1 << bit)].add(sid)
        self.local_groups = []
        for root in roots:
            seen, todo = set(), [root]
            while todo:
                sid = todo.pop()
                if sid in seen: continue
                seen.add(sid)
                spell = self.spells.get(sid)
                if not spell or spell['_proc_flags']: continue
                todo.extend(e['_trigger_spell_id'] for e in self.by_spell.get(sid, [])
                            if e['_type'] == 64 and e['_trigger_spell_id'])
            if seen & self.damage:
                self.local_groups.append((root, seen))
        # 颜色使用 SimC 实际读取的 DBC rank 字段，非效果描述。
        self.colors = defaultdict(set)
        for line in (source / 'engine/dbc/generated/spelltext_data.inc').read_text(encoding='utf-8').splitlines():
            if not line.startswith('  {'): continue
            row = aggregate(line)
            if len(row) == 4 and row[3] in ('Black', 'Blue', 'Bronze', 'Green', 'Red'):
                self.colors[row[3]].add(row[0])

    def resolve(self, e, bindings, scope, reads=()):
        evidence = {'依据版本': self.revision, '判断输入': 'DBC 类型、选择位、标签、触发关系及原生注册；不使用描述'}
        def result(decision, reason, **facts):
            return decision, reason, {**evidence, **facts}
        if scope == '公共伤害乘区':
            return result('应剔除', '原生代码登记在角色或宠物的公共伤害修正器中。', 判定路径='公共修正器')
        if e['type'] in (6, 35, 65) and e['subtype'] in CATEGORY_AURAS and not any(e['flags']) and not e['affected_spells']:
            category = CATEGORY_AURAS[e['subtype']]
            return result('应剔除', 'DBC 明确修正整个' + category + '类别，属于全局效果。', 判定路径='DBC 类别类型', 类别=category, 效果类型=e['subtype'], 范围参数=e['misc1'])
        targets = {s['spell_id'] for s in e['affected_spells']}
        native_targets = {sid for b in bindings for sid in b.get('传递到的伤害技能', []) if sid}
        local = LOCAL_SELECTORS.get((e['class_family'], tuple(e['flags']))) if self.revision == REVIEWED_REVISION else None
        if local and e['subtype'] in (107,108) and targets == local['ids'] and native_targets <= targets:
            text = (self.source/local['file']).read_text(encoding='utf-8')
            if all(marker in text for marker in local['markers']):
                sites=[]
                for marker in local['markers']:
                    pos=text.index(marker);line=text[:pos].count('\n')+1
                    sites.append({'文件':local['file'],'行':line,'代码':'\n'.join(text[pos:].splitlines()[:6])})
                return result('保留', '选择器明确限定' + local['label'] + '，原生代码也有对应的具体技能实现。',
                              判定路径='已复核具体技能集合', 完整DBC集合=sorted(targets), 源码=sites)
        for read in reads:
            function = read.get('伤害函数') or ''
            if '::composite_player' not in function: continue
            text = (self.source/read['文件']).read_text(encoding='utf-8')
            from simc_native_scope_evidence import damage_function_spans
            for start, end, name in damage_function_spans(text):
                if name != function: continue
                body = text[start:end]
                if not (start <= sum(len(line)+1 for line in text.splitlines()[:read['行']-1]) < end): continue
                if re.search(r'->action|\.action|\bid\s*\(|\bdata\s*\(|spell_id|internal_id', body): continue
                return result('应剔除', '原生公共伤害函数直接使用该分量，未按具体技能身份筛选。',
                              判定路径='公共伤害函数直接读取', 文件=read['文件'], 行=read['行'], 函数=function, 代码=body)
        if self.revision == REVIEWED_REVISION and e['subtype'] in (107, 108, 271) and e['misc1'] in (0, 7, 15, 22):
            selector = COMMON_SELECTORS.get(e['class_family'])
            if selector and e['flags'][selector[0]] & selector[1]:
                i, bit, name = selector
                members = sorted(s['_id'] for s in self.spells.values()
                                 if s['_class_flags_family'] == e['class_family'] and s['_class_flags'][i] & bit)
                if not set(members) <= targets:
                    return result('待确认', 'DBC 完整集合与已复核的公共选择器不一致。', 判定路径='选择器校验失败')
                return result('应剔除', '使用' + name + '的公共掩码；不是限定某几个具名技能。',
                              判定路径='已复核公共掩码', 职业族=e['class_family'], 选择位=[i, bit],
                              完整DBC集合=members, 原生伤害字段=sorted({b['field'] for b in bindings}),
                              代码附加技能=sorted(native_targets-targets))
            if e['class_family'] == 10 and e['flags'][1] & 16384 and e['flags'][2] & 2:
                return result('应剔除', '使用圣骑士公共伤害与治疗选择位的组合；伤害分量属于全局效果。', 判定路径='已复核公共掩码', 选择位=[[1,16384],[2,2]], 完整DBC集合=sorted(targets))
        if e['class_family'] == 224 and e['method'] == 'dbc.spells_by_label':
            categories = {color:{sid for sid in ids if sid in self.damage and self.spells[sid]['_class_flags_family']==224}
                          for color,ids in self.colors.items()}
            for color, members in categories.items():
                if members and members <= targets and not any(targets & other for key, other in categories.items() if key != color):
                    return result('应剔除', 'DBC 标签覆盖整个龙族颜色类别（' + color + '），不是几个具名技能。',
                                  判定路径='DBC 颜色类别与原生颜色逻辑', 标签=e['misc2'], 颜色=color, 颜色法术=sorted(members), 完整DBC集合=sorted(targets))
        if self.revision == REVIEWED_REVISION and e['subtype'] in (107,108,218,219,271,537):
            for word, bit, category in CATEGORY_SELECTORS.get(e['class_family'], []):
                if e['flags'][word] & bit:
                    members = self.flag_members[(e['class_family'], word, bit)]
                    if members and members <= targets:
                        return result('应剔除', 'DBC 使用整个'+category+'类别的选择位，属于全局效果。',
                                      判定路径='DBC 类别选择位', 类别=category, 选择位=[word,bit], 完整DBC集合=sorted(targets))
        if e['method'] in ('dbc.effect_affects_spells','dbc.spells_by_label','spell_data_t.affected_by_all') and targets:
            universe = getattr(self, 'damage_families', {}).get(e['class_family'], set())
            affected = (targets | native_targets) & self.damage
            if universe and universe <= affected:
                return result('应剔除', 'DBC 与原生应用合并后覆盖该职业族全部伤害法术。',
                              判定路径='完整伤害集合覆盖', 完整DBC集合=sorted(targets), 全部伤害法术=sorted(universe))
            if universe and affected and universe - affected:
                # 直接按 DBC 限定范围处理，不再要求人工局部证书、单一技能根或已选天赋。
                return result('保留', 'DBC 选择器限定部分技能；未命中的伤害技能不受这个分量影响。',
                              判定路径='DBC 部分技能选择器', 完整DBC集合=sorted(targets),
                              原生附加技能=sorted(native_targets-targets),
                              未命中的伤害技能=sorted(universe-affected),
                              判断规则='已知公共类别优先；其余按 DBC 与原生应用的部分技能范围保留')
        if e['type'] in (2,9,31,58,121) or (e['type']==6 and e['subtype'] in (3,53,89)):
            return result('保留', '这是该技能自身的伤害分量，不是对全部技能的伤害加成。',
                          判定路径='技能自身伤害', 伤害技能=[e['source_spell_id']])
        if native_targets:
            universe = getattr(self, 'damage_families', {}).get(e['class_family'], set())
            if universe - native_targets:
                return result('保留', '原生伤害修正登记在列出的部分技能字段上，未登记为角色公共乘区。',
                              判定路径='原生部分技能登记', 原生伤害技能=sorted(native_targets),
                              未命中的伤害技能=sorted(universe-native_targets))
        # 掩码和标签使用同一局部判定入口；先排除公共乘区与类别选择器。
        if e['subtype'] in (107, 108, 218, 219) and targets and native_targets <= targets:
            certified = self.certified_skill_set(e, targets)
            if certified:
                return result('保留', 'DBC 完整选择范围限定这些具体技能及其变体，SimC 中有对应的技能实现；多个独立技能仍属于局部效果。',
                              判定路径='完整具名技能集合', 完整DBC集合=sorted(targets), 技能集合核验=certified,
                              配置覆盖要求='不要求当前配置逐个实例化；范围结论与触发条件分别判断')
            if len(targets) == 1:
                return result('保留', 'DBC 完整选择器只限定这一个具体法术，现有原生注册未发现范围扩展。',
                              判定路径='单一具体法术选择器', 完整DBC集合=sorted(targets), 原生附加技能=[])
            roots = [(root, group) for root, group in self.local_groups if targets <= group]
            if roots:
                root, group = min(roots, key=lambda r:len(r[1]))
                return result('保留', 'DBC 只选择具体技能及其直接施法触发分量。',
                              判定路径='具体技能及纯施法触发链', 技能根=root, 完整DBC集合=sorted(targets), 触发链=sorted(group))
            parts = []
            for word, mask in enumerate(e['flags']):
                for bit in range(32):
                    if not mask & (1 << bit): continue
                    members = self.flag_members[(e['class_family'], word, 1 << bit)]
                    if not members: continue
                    groups = [(root, group) for root, group in self.local_groups if members <= group]
                    if len(members) != 1 and not groups:
                        return None
                    root = next(iter(members)) if len(members)==1 else min(groups,key=lambda item:len(item[1]))[0]
                    parts.append({'选择位':[word,1<<bit], '具体技能根':root, '法术集合':sorted(members)})
            if parts and set().union(*(set(part['法术集合']) for part in parts)) == targets:
                return result('保留', '每个选择位分别指向具体技能或其施法分量，合并后仍是明确的技能集合。',
                              判定路径='具体技能选择位的并集', 选择位分解=parts, 完整DBC集合=sorted(targets))
        return None

    def certified_skill_set(self, e, targets):
        """允许任意多个独立技能；证书校验完整 DBC 集合与同版源码后才生效。"""
        if self.revision != REVIEWED_REVISION:
            return None
        parts = []
        covered_flags = [0, 0, 0, 0]
        covered_targets = set()
        for record in getattr(self, 'skill_sets', []):
            if record['职业族'] != e['class_family']:
                continue
            flags = record['选择位']
            # 多个局部选择器的并集也可能恰好成为整类伤害；只接受已复核的完整签名。
            if e['flags'] != flags:
                continue
            if not any(flags):
                continue
            members = set().union(*(self.flag_members[(e['class_family'], word, 1 << bit)]
                                    for word, mask in enumerate(flags) for bit in range(32) if mask & (1 << bit)))
            if members != set(record['完整DBC集合']) or not members <= targets:
                continue
            sites = []
            for site in record['源码身份']:
                text = (self.source / site['文件']).read_text(encoding='utf-8')
                if hashlib.sha256(text.encode()).hexdigest() != site['文本摘要']:
                    break
                pos = text.find(site['定位代码'])
                if pos < 0:
                    break
                sites.append({'文件':site['文件'], '行':text[:pos].count('\n') + 1,
                              '代码':'\n'.join(text[pos:].splitlines()[:24])})
            else:
                if not sites:
                    continue
                parts.append({'技能集合':record['技能集合'], '选择位':flags,
                              '完整DBC集合':sorted(members), '源码':sites})
                covered_flags = [a | b for a, b in zip(covered_flags, flags)]
                covered_targets.update(members)
        if covered_flags == e['flags'] and covered_targets == targets:
            return parts
        return None
