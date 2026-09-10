"""提取原生伤害函数所属的 C++ 类型和函数正文，保留原始位置。"""
import re
from functools import lru_cache


def clean_cpp(text):
    return re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), text)


@lru_cache(maxsize=64)
def class_spans(text):
    cleaned = clean_cpp(text)
    spans = []
    for m in re.finditer(r'\b(?:struct|class)\s+(\w+)\s*((?:final\s*)?(?::[^;{}]*)?)\{', cleaned):
        if re.search(r'enum\s*$', cleaned[max(0,m.start()-10):m.start()]):
            continue
        depth, end = 1, m.end()
        while end < len(cleaned) and depth:
            depth += (cleaned[end] == '{') - (cleaned[end] == '}')
            end += 1
        if depth == 0:
            spans.append((m.start(), end, m[1], m[2]))
    return spans


def read_scope(source, read):
    from simc_native_scope_evidence import damage_function_spans
    text = (source/read['文件']).read_text(encoding='utf-8')
    offset = sum(len(line)+1 for line in text.splitlines()[:read['行']-1])
    function = next((s for s in damage_function_spans(text) if s[0] <= offset < s[1]), None)
    if not function:
        return None
    owners = [s for s in class_spans(text) if s[0] <= offset < s[1]]
    owner = min(owners, key=lambda s:s[1]-s[0]) if owners else None
    qualified = function[2].split('::')
    name = qualified[-2] if len(qualified)>1 else owner[2] if owner else None
    if not name:
        return None
    return {'类型':name, '函数':function[2], '文件':read['文件'],
            '外层类型':[s[2] for s in owners if not owner or s != owner],
            '行':text[:function[0]].count('\n')+1,
            '代码':text[function[0]:function[1]],
            '类型声明':text[owner[0]:min(owner[1],owner[0]+1000)] if owner else '',
            '父类':owner[3] if owner else ''}


def enclosing_type(text, offset):
    owners = [s for s in class_spans(text) if s[0] <= offset < s[1]]
    return min(owners, key=lambda s:s[1]-s[0]) if owners else None


def generic_action(scope):
    return bool(re.search(r'parse_action_effects_t|\b(?:Base|BASE)\b', scope['父类']) or
                ('affected_by.' in scope['代码'] and re.search(r'\w+_action_t\s*<\s*(?:spell_t|attack_t|heal_t)', scope['父类'])))


def non_damage_read(scope):
    # 依据实际继承的治疗类型；函数名中有 multiplier 并不能证明它修改伤害。
    return bool(re.search(r'\b\w*heal_t\b', scope['父类'])) or bool(
        re.search(r'\btarget\s*=\s*player\s*;', clean_cpp(scope['类型声明'])))


def classify_reads(source, reads):
    sites = [read_scope(source, r) for r in reads if r.get('伤害函数')]
    sites = [s for s in sites if s and not non_damage_read(s)]
    if not sites:
        return None
    generic = [s for s in sites if generic_action(s)]
    if not generic:
        if any('composite_player' in s['函数'] for s in sites):
            return '应剔除', '原生角色或宠物类型的公共伤害函数使用该分量。', sites
        return '保留', '原生代码只在列出的具体技能实现中读取该分量，属于部分技能效果。', sites
    assignment_sites = []
    for scope in generic:
        relevant = [r for r in reads if r['文件']==scope['文件'] and r.get('伤害函数')==scope['函数']]
        flags = set()
        for read in relevant:
            found = re.findall(r'affected_by\.(\w+)', read['代码'])
            if not found:
                token = read['符号'].split('.')[-1]
                if 'affected_by.'+token in scope['代码']:
                    found = [token]
            flags.update(found)
        text=(source/scope['文件']).read_text(encoding='utf-8')
        if not flags:
            # 此类技能包装器只被具体类型继承，不是全职业动作基类。
            name=scope['类型']
            uses=[s for s in class_spans(text) if re.search(r'\b'+re.escape(name)+r'\s*<', s[3])]
            if uses:
                assignment_sites.extend({'文件':scope['文件'],'行':text[:s[0]].count('\n')+1,
                                         '类型':s[2],'代码':text[s[0]:s[0]+500]} for s in uses)
                continue
            return None
        for flag in flags:
            if re.search(r'\bbool\s+'+re.escape(flag)+r'\s*=\s*true\b', clean_cpp(text)):
                return '应剔除', '原生通用伤害基类默认对技能启用该伤害分量。', sites
            matches=list(re.finditer(r'\baffected_by\.'+re.escape(flag)+r'(?:\.\w+)?\s*=\s*([^;]+);', text))
            enabled=[m for m in matches if m[1].strip()!='false']
            if not enabled:
                return None
            for match in enabled:
                owner=enclosing_type(text,match.start())
                if not owner:
                    return None
                assignment={'文件':scope['文件'],'行':text[:match.start()].count('\n')+1,
                            '类型':owner[2],'代码':match[0]}
                assignment_sites.append(assignment)
                if owner[2]==scope['类型']:
                    if 'get_school_mask' in match[1]:
                        return '应剔除', '原生通用伤害基类按伤害学校筛选整个类别。', sites+assignment_sites
                    return None
    return '保留', '原生通用函数由具体技能中的开关或限定技能包装器启用，属于部分技能效果。', sites+assignment_sites


def referenced_dbc_scope(resolver, effect, reads):
    """追踪通用伤害函数开关借用的其他 DBC 效果，不按来源天赋 ID 设置结论。"""
    from simc_native_scope_evidence import source_class
    targets, sites, mastery = set(), [], False
    scopes = [(r,read_scope(resolver.source,r)) for r in reads if r.get('伤害函数')]
    for read, scope in scopes:
        if not scope or not generic_action(scope):
            continue
        flags=set(re.findall(r'affected_by\.(\w+)',read['代码']))
        if not flags:
            token=read['符号'].split('.')[-1]
            if 'affected_by.'+token in scope['代码']:flags.add(token)
        text=(resolver.source/read['文件']).read_text(encoding='utf-8')
        cid=source_class(resolver.source/read['文件'])
        units=[(p,p.read_text(encoding='utf-8')) for p in (resolver.source/'engine/class_modules').rglob('*')
               if p.suffix in ('.cpp','.hpp') and source_class(p)==cid]
        for flag in flags:
            for match in re.finditer(r'affected_by\.'+re.escape(flag)+r'\s*=\s*([^;]+);',text):
                refs=re.findall(r'(\w+(?:\.\w+)+)->effectN\(\s*(\d+)\s*\)',match[1])
                for symbol, index in refs:
                    found=set()
                    for file, unit in units:
                        pattern=r'\b'+re.escape(symbol)+r'\s*=\s*find_(spell|specialization_spell|mastery_spell)\(\s*("[^"]+"|\d+|\w+)'
                        for binding in re.finditer(pattern,unit):
                            kind,value=binding[1],binding[2]
                            if kind=='spell' and value.isdigit():found.add(int(value))
                            elif kind=='specialization_spell' and value.startswith('"'):
                                found.update(sid for sid in resolver.roots if resolver.spells.get(sid,{}).get('_name')==value[1:-1]
                                             and resolver.spells[sid]['_class_flags_family']==effect['class_family'])
                            elif kind=='mastery_spell':
                                enums=(resolver.source/'engine/dbc/generated/sc_specialization_data.inc').read_text(encoding='utf-8')
                                enum=re.search(r'\b'+re.escape(value)+r'\s*=\s*(\d+)',enums)
                                data=(resolver.source/'engine/dbc/generated/mastery_spells.inc').read_text(encoding='utf-8')
                                entry=re.search(r'\{\s*'+enum[1]+r'\s*,\s*(\d+)\s*\}',data) if enum else None
                                if entry:found.add(int(entry[1]));mastery=True
                            sites.append({'文件':str(file.relative_to(resolver.source)).replace('\\','/'),
                                          '行':unit[:binding.start()].count('\n')+1,'代码':binding[0]})
                    if len(found)!=1:
                        return None
                    sid=next(iter(found))
                    eff=next((e for e in resolver.by_spell[sid] if e['_index']==int(index)-1),None)
                    if not eff or not any(eff['_class_flags']):return None
                    members={i for i,s in resolver.spells.items() if s['_class_flags_family']==resolver.spells[sid]['_class_flags_family']
                             and any(a&b for a,b in zip(s['_class_flags'],eff['_class_flags']))}
                    targets.update(members)
                    sites.append({'文件':read['文件'],'行':text[:match.start()].count('\n')+1,'代码':match[0],
                                  '引用法术ID':sid,'引用效果':int(index),'完整DBC集合':sorted(members)})
    if targets:
        decision='应剔除' if mastery else '保留'
        reason='原生通用伤害函数借用精通的整类伤害范围。' if mastery else '原生函数借用的 DBC 选择器限定部分技能，已追踪到完整技能集合。'
        return decision,reason,{'判定路径':'原生开关引用DBC','源码':sites,'完整DBC集合':sorted(targets)}
    return None
