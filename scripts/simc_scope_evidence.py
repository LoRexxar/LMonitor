"""独立审阅局部作用域证据；未识别为全局不构成局部证据。"""
import re


def text_variants(text, limit=128):
    """保留条件分支的边界，超限或不完整表达式不据此确认局部。"""
    pending, output = [text], []
    while pending:
        current = pending.pop()
        start = current.find('$?')
        if start < 0:
            output.append(current)
            continue
        cursor, branches = start + 1, []

        def bracket(pos):
            if pos >= len(current) or current[pos] != '[':
                raise ValueError('条件分支不完整')
            begin, depth = pos + 1, 1
            pos += 1
            while pos < len(current) and depth:
                depth += (current[pos] == '[') - (current[pos] == ']')
                pos += 1
            if depth:
                raise ValueError('条件分支不完整')
            return current[begin:pos-1], pos

        try:
            while cursor < len(current) and current[cursor] == '?':
                cursor = current.index('[', cursor)
                branch, cursor = bracket(cursor)
                branches.append(branch)
            branch, cursor = bracket(cursor)
            branches.append(branch)
        except ValueError:
            continue
        if len(pending) + len(output) + len(branches) > limit:
            return []
        pending.extend(current[:start] + branch + current[cursor:] for branch in branches)
    return sorted(set(output))


def outgoing_damage_declaration(text):
    """承伤、吸收与治疗出现 damage 一词，不能因此进入输出伤害清单。"""
    for clause in re.split(r'[.;\r\n]', text):
        incoming=re.search(r'\b(?:damage\s+taken|damage\s+(?:you|they)(?:\s+would)?\s+take|damage\s+(?:done\s+)?to\s+you|absorb\w*|heal(?:s|ed|ing)?|prevent\w*|avoid\w*|withstand)\b',clause,re.I)
        if incoming and not re.search(r'\bdeals?\b[^,;]{0,100}\b(?:more\s+)?damage|damage\s+(?:taken\s+)?from\s+you\b',clause,re.I):
            continue
        if re.search(r'\b(?:deal(?:s|ing)?\b[^.;]{0,140}\bdamage|damaging|bleed|damage\s+(?:of|done|dealt|by|is|increased|reduced)|increas\w*\b[^.;]{0,140}\bdamage)',clause,re.I):
            return True
    return False


def damage_parts(row):
    """从原始效果取清单候选，不读取分类器的全局判断。"""
    text = (row.get('description') or '') + '\n' + (row.get('tooltip') or '')
    declaration = outgoing_damage_declaration(text)
    parts = []
    for e in row.get('effects', []):
        if not e.get('id'):
            continue
        direct = e['type'] in (2, 9, 31, 121)
        modifier = e['type'] in (6, 35, 174) and (
            e['subtype'] in (3, 79, 163, 168, 276, 303, 344, 429, 531, 270, 271, 295, 501, 537)
            or (e['subtype'] in (108, 218) and e['misc1'] in (0, 15, 22)))
        # 脚本分量只在伤害句中引用其数值时待核对，不把任意触发边当成伤害。
        ref = r'\$(?:' + str(row['spell_id']) + r')?[swm]' + str(e['index']) + r'\b'
        referring=[clause for clause in re.split(r'[.;\r\n]',text) if re.search(ref,clause,re.I)]
        if referring and not any(outgoing_damage_declaration(clause) for clause in referring):
            modifier=False
        scripted = e['type'] in (6, 35, 174) and e['subtype'] == 4 and any(
            outgoing_damage_declaration(clause) and re.search(
                r'\b(?:deals?|dealing)\s+' + ref + r'%\s+(?:(?:of|more|less|increased|additional|the|its|their|your)\s+){0,4}damage\b', clause, re.I)
            for clause in re.split(r'[.;\r\n]', text))
        if direct or (declaration and e['value'] != 0 and (modifier or scripted)):
            parts.append(e['index'])
    return sorted(set(parts))


def local_scope_evidence(row, spell_names):
    """只接受技能本体伤害或具名技能集合与数值的直接绑定。"""
    result = {}
    effects = {e['index']: e for e in row.get('effects', [])}
    for index in damage_parts(row):
        if effects[index]['type'] in (2, 9, 31, 121):
            result[index] = {'依据': '该法术自身的直接伤害效果', '具名技能': [row['name']]}
    text = (row.get('description') or '') + '\n' + (row.get('tooltip') or '')
    text = re.sub(r'\$@spellname(\d+)', lambda m: spell_names.get(int(m[1]), m[0]), text)
    # 自动攻击、学校和伤害类别即使恰有同名法术，也不能充当具名技能证据。
    categories = re.compile(r'^(?:auto.?attacks?|auto.?shots?|attacks?|damage|bleeds?|pets?|demons?|fire|frost|shadow|holy|arcane|nature|physical|cosmic|magic|periodic|critical strike)s?$', re.I)
    present = sorted({name for name in spell_names.values() if name and not categories.fullmatch(name)
                      and name in text and re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text)}, key=len, reverse=True)
    if not present:
        return result
    name = '(?:' + '|'.join(map(re.escape, present)) + ')'
    names = name + r'(?:(?:,\s*(?:and\s+)?|\s+(?:and|or)\s+)' + name + ')*'
    value = r'(?:\$\{[^}]+\}|\$(?:\d+)?[swm]\d+)'
    patterns = [
        r'(?<!\w)(?:Your\s+)?(?P<names>' + names + r")(?:'s)?\s+(?:critical\s+(?:strike\s+)?damage|damage)(?:\s+(?:dealt|done))?\s+(?:is\s+)?(?:increased|reduced)\s+by\s+" + value,
        r'(?<!\w)(?P<names>' + names + r')\s+deals?\s+' + value + r'%\s+(?:increased|additional|more|less|reduced)\s+(?:critical\s+(?:strike\s+)?)?damage',
        r'\bIncreases?\s+(?:the\s+)?damage\s+(?:(?:done|dealt)\s+)?(?:of|by)\s+(?P<names>' + names + r')\s+by\s+' + value,
    ]
    for variant in text_variants(text):
        for pattern in patterns:
            for match in re.finditer(pattern, variant, re.I):
                bound_names = [n for n in present if re.search(r'(?<!\w)' + re.escape(n) + r'(?!\w)', match['names'], re.I)]
                for ref in re.finditer(r'\$(\d*)[swm](\d+)', match[0], re.I):
                    if ref[1] and int(ref[1]) != row['spell_id']:
                        continue
                    index = int(ref[2])
                    if index in damage_parts(row):
                        result[index] = {'依据': '伤害数值直接绑定明确具名技能集合', '具名技能': bound_names, '原文': match[0]}
    # 只扩展完全相同的非空技能掩码或标签，不以相同数值推测相同作用域。
    for index, e in effects.items():
        if index in result or index not in damage_parts(row):
            continue
        for anchor, evidence in list(result.items()):
            a = effects[anchor]
            if e['type'] == a['type'] == 6 and e['subtype'] == a['subtype'] and e['subtype'] in (108, 218) and (
                any(e['flags']) or e['misc2']) and e['flags'] == a['flags'] and e['misc2'] == a['misc2'] and e['value'] == a['value']:
                result[index] = {**evidence, '对应具名分量': anchor}
                break
    return result


def verify_independent_expectations(catalog, fixture):
    """手工审阅的正反例期望与分类输出比较；缺少声明也必须报错。"""
    rows = {r['spell_id']: r for r in catalog['talent_catalog']}
    errors = []
    for case in fixture['cases']:
        row = rows.get(case['spell_id'])
        if row is None:
            errors.append(f"独立样本缺失：{case['spell_id']}")
            continue
        if row['description'] != case['description'] or row['effects'] != case['effects']:
            errors.append(f"独立样本源数据发生变化，须重新审阅：{case['spell_id']}")
            continue
        expected, actual = set(case['expected']), set(row['global_effect_indices'])
        if expected != actual:
            errors.append(f"独立作用域预期不符 {case['spell_id']}：漏提取 {sorted(expected-actual)}；误提取 {sorted(actual-expected)}")
    return errors
