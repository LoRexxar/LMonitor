"""只处理 DBC 关系、原生注册记录和源码引用；不使用技能描述分类。"""
from collections import defaultdict
from pathlib import Path
import re

CLASS_FILES={'warrior':1,'paladin':2,'hunter':3,'rogue':4,'priest':5,'death_knight':6,
             'shaman':7,'mage':8,'warlock':9,'monk':10,'druid':11,'demon_hunter':12,'evoker':13}
DAMAGE_FIELDS={'direct_damage','periodic_damage','crit','crit_chance','crit_bonus','target_multiplier',
               'player_multiplier','pet_multiplier','auto_attack_speed','damage_multiplier',
               'damage','damage_pct','damage_done','damage_school'}
PERIOD_FIELDS={'duration','tick_time','period'}
DAMAGE_HOOKS={'composite_player_multiplier','composite_player_target_multiplier',
              'composite_player_critical_damage_multiplier','action_multiplier',
              'composite_da_multiplier','composite_ta_multiplier','composite_target_multiplier',
              'composite_crit_damage_bonus_multiplier','composite_crit_multiplier','n_targets'}


def damage_function_spans(text):
    """只识别明确的原生伤害函数边界，不把附近注释作为伤害证据。"""
    cleaned=re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                   lambda m:''.join('\n' if c=='\n' else ' ' for c in m[0]),text)
    spans=[]
    pattern=r'\b(?:double|int|unsigned)\s+([\w:]+)\s*\([^{};]*\)\s*(?:(?:const|override|final|noexcept)\s*)*\{'
    for match in re.finditer(pattern,cleaned):
        name=match[1].split('::')[-1]
        if name not in DAMAGE_HOOKS:continue
        depth,pos=1,match.end()
        while pos<len(cleaned) and depth:
            depth+=(cleaned[pos]=='{')-(cleaned[pos]=='}')
            pos+=1
        if depth==0:spans.append((match.start(),pos,match[1]))
    return spans


def source_class(path):
    parts=path.as_posix().split('/')
    for token,cid in CLASS_FILES.items():
        if any(p==token or p.startswith('sc_'+token+'.') or p.startswith('sc_'+token+'_') for p in parts):
            return cid
    return None


def index_native_reads(source, catalog):
    """通过源码中的天赋注册建立符号身份；引用不是分支执行证明。"""
    names=defaultdict(set)
    for row in catalog['talent_catalog']:
        names[(row['class_id'],row['name'].casefold())].add(row['spell_id'])
    units=[]
    for path in (source/'engine/class_modules').rglob('*'):
        if path.suffix not in ('.cpp','.hpp'):continue
        cid=source_class(path)
        if not cid:continue
        units.append((path,cid,path.read_text(encoding='utf-8')))
    symbols=defaultdict(set)
    registration=re.compile(r'\b(talents?(?:\.\w+)+)\s*=\s*[^;]{0,350}?"([^"\n]+)"')
    for _,cid,text in units:
        for match in registration.finditer(text):
            symbols[(cid,match[1])].update(names.get((cid,match[2].casefold()),()))
        # 目标减益常通过 find_spell 数字身份保存，不能只索引天赋对象。
        for match in re.finditer(r'\b((?:spells?|spec)(?:\.\w+)+)\s*=\s*(?:\w+->)?find_spell\(\s*(\d+)\s*\)',text):
            symbols[(cid,match[1])].add(int(match[2]))
    output=defaultdict(list)
    for path,cid,text in units:
        lines=text.splitlines()
        spans=damage_function_spans(text)
        position=0
        for lineno,line in enumerate(lines,1):
            line_position=position
            position+=len(line)+1
            code=line.split('//',1)[0]
            if not code.strip():continue
            for match in re.finditer(r'\b((?:talents?|spells?|spec)(?:\.\w+)+)\s*->\s*effectN\(\s*(\d+)\s*\)',code):
                ids=symbols.get((cid,match[1]),())
                if len(ids)!=1:continue
                sid=next(iter(ids))
                context='\n'.join(lines[max(0,lineno-4):min(len(lines),lineno+2)])
                functions=[name for start,end,name in spans if start<=line_position<end]
                output[(sid,int(match[2]))].append({'文件':str(path.relative_to(source)).replace('\\','/'),
                    '行':lineno,'符号':match[1],'伤害函数':functions[-1] if functions else None,
                    '代码':context,'证明范围':'源码直接读取该效果；不自动推断分支已执行或全局范围'})
    return output


def binding_has_damage(binding, effects, damage_ids):
    """用原生伤害字段与目标类型判断，不能把治疗法术的通用修饰器混入。"""
    field=binding['field']
    source=effects.get(binding['effect_id'],{})
    target=binding['target_spell_id']
    dbc_targets={s['spell_id']:s for s in source.get('affected_spells',[])}
    is_damage=target in damage_ids or bool(dbc_targets.get(target,{}).get('dbc_direct_damage')) or bool(dbc_targets.get(target,{}).get('dbc_periodic_damage'))
    if binding['layer'] in ('player_registry','passive_player','target_player_registry'):
        return field in DAMAGE_FIELDS
    if field in DAMAGE_FIELDS:
        return is_damage
    if field in PERIOD_FIELDS:
        return is_damage
    if field.startswith('effect_'):
        # 改写子效果可能是治疗、资源、伤害；保留关联证据但不猜伤害类型。
        return False
    return False


def relation_has_damage(relation):
    subtype,prop=relation['subtype'],relation['misc1']
    if relation['type'] in (2,9,31,58,121) or (relation['type']==6 and subtype in (3,53,89)):return True
    if subtype in (107,108,218,219) and prop in (0,7,15,17,22,40):
        return any(s.get('dbc_direct_damage') or s.get('dbc_periodic_damage') for s in relation['affected_spells'])
    return subtype in (79,163,168,276,303,319,344,429,531) and relation['base_value']!=0


def structural_scope(relation, bindings):
    """只给结构证据能支持的范围，不用技能数量或名称判断类别。"""
    if relation['type'] in (2,9,31,58,121) or (relation['type']==6 and relation['subtype'] in (3,53,89)):
        return '技能本体伤害', 'DBC 声明该法术自身的伤害分量；触发来源是否属于全局效果须另看原生触发逻辑。'
    if any(b['layer'] in ('player_registry','passive_player','target_player_registry') and b['field'] in DAMAGE_FIELDS for b in bindings):
        return '公共伤害乘区', '原生玩家或宠物公共修正器实际登记该效果；具体条件见注册记录与源码。'
    if relation['subtype'] in (79,319,344,429) and relation['method']=='no_static_spell_selector':
        return '伤害类别修正', 'DBC 的效果类型直接指向伤害学校、自动攻击或宠物类别；不依赖技能描述。'
    if bindings:
        return '已解析技能应用关系', '已取得原生实际修改的技能和字段；DBC 集合与代码覆盖项分别列出，不能仅以集合大小判断是否全局。'
    if relation['affected_spells']:
        return 'DBC 范围已解析，原生应用未覆盖', '已按 SimC 的 DBC 索引解析技能集合；当前配置未登记此效果，不能据此断言无影响。'
    return '需追踪手写逻辑', '通用 DBC 选择器未提供技能集合，需沿原生读取及触发关系继续追踪。'


def validate_native_payload(data):
    if data.get('normalized') is not False or data.get('evidence_schema')!=1:
        raise ValueError('必须使用正常初始化、未经全局清除的原生证据')
    for actor in data['actors']:
        effects={e['effect_id']:e for e in actor['source_effects']}
        for b in actor['bindings']:
            e=effects.get(b['effect_id'])
            if not e or e['source_spell_id']!=b['source_spell_id'] or e['effect_index']!=b['effect_index']:
                raise ValueError('原生应用关系缺少对应的 DBC 源效果，不能猜测绑定')


def native_damage_bindings(actor):
    """沿实际登记的效果改写边追溯到伤害字段，不以数值相同推断关联。"""
    effects={e['effect_id']:e for e in actor['source_effects']}
    keys={(e['source_spell_id'],e['effect_index']):e['effect_id'] for e in effects.values()}
    damage_ids={a['spell_id'] for a in actor['damage_actions'] if a['spell_id']}
    relevant={}
    for i,b in enumerate(actor['bindings']):
        if binding_has_damage(b,effects,damage_ids):
            relevant[i]={**b,'传递到的伤害技能':[b['target_spell_id']] if b['target_spell_id'] else []}
    while True:
        known=defaultdict(set)
        for b in relevant.values():known[b['effect_id']].update(b['传递到的伤害技能'])
        new={}
        for i,b in enumerate(actor['bindings']):
            if i in relevant or b['layer']!='passive_effect' or not b['field'].startswith('effect_'):continue
            target=keys.get((b['target_spell_id'],int(b['field'].split('_')[1])))
            if target in known:
                new[i]={**b,'下游效果ID':target,'传递到的伤害技能':sorted(known[target])}
        if not new:break
        relevant.update(new)
    return list(relevant.values())
