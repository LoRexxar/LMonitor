"""从实际编译使用的作用域契约导出可审阅数据，不根据名称或描述重新分类。"""
import argparse
import hashlib
import json
import re
from pathlib import Path


def export_tables(source):
    """保留原始事实与身份，并单独列出统一的产品处理规则。"""
    def constant(name):
        return re.search(rf'{name} = "([^"]+)";', source)[1]

    def array(name):
        return source.split(name + '[] = {', 1)[1].split('\n};', 1)[0]

    components = []
    for line in array('skill_damage_scope_effects').splitlines():
        match = re.fullmatch(r'\s*\{(\d+), (\d+), (\d+), (\d+), (true|false)\},', line)
        if not line.strip():
            continue
        if not match:
            raise ValueError('无法解析分量表，拒绝跳过未知格式。')
        spell, index, effect, family = map(int, match.groups()[:4])
        components.append({'源法术ID': spell, '效果编号': index, '效果ID': effect,
                           '职业族': family, '判定': '全局' if match[5] == 'true' else '保留'})
    component_index = {(r['源法术ID'], r['效果编号']): r for r in components}
    if len(component_index) != len(components):
        raise ValueError('分量身份重复。')
    rows, identities = [], set()
    for line in array('skill_damage_scope_display').splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'\s*\{(\d+), "([^"]+)", (".*")\},', line)
        if not match:
            raise ValueError('无法解析展示表，拒绝跳过未知格式。')
        fact = json.loads(json.loads(match[3]))
        identity = (fact['effect_id'], match[2])
        if identity in identities:
            raise ValueError('来源与专精身份重复。')
        identities.add(identity)
        if fact['specializations'] != [match[2]]:
            raise ValueError('原生专精与展示专精不一致。')
        for part in fact['global_components']:
            component = component_index[(part['spell_id'], part['effect_index'])]
            if component['判定'] != '全局' or component['效果ID'] != part['effect_id']:
                raise ValueError('展示表与实际分量分类不一致。')
        local_components = fact.get('local_components', [])
        local_bindings = fact.get('local_skill_bindings', [])
        has_bound_skill = any(binding.get('skill_spell_ids') for binding in local_bindings)
        integrity = '完整' if not fact.get('partial_state') or has_bound_skill else '缺少局部技能绑定'
        rows.append({'身份': fact['effect_id'], '职业': fact['source_class'],
                     '专精': match[2], '类型': {'talent': '天赋', 'buff': '自身增益',
                                               'debuff': '目标减益'}[fact['source_kind']],
                     '名称': fact['display_name'], '法术ID': fact['source_spell_ids'],
                     '判定': '全局', '上方全局表': '显示', '下方技能条件': '排除',
                     '原契约混合标记': fact.get('partial_state', False),
                     '全局分量': fact['global_components'],
                     '局部分量': local_components,
                     '局部技能绑定': local_bindings,
                     '局部证据': fact.get('local_scope_evidence'),
                     '数据完整性': integrity,
                     '下表规则': fact.get('lower_skill_policy', 'exclude_global_keep_explicit_local'),
                     '加成数据': fact.get('effect_details', []),
                     '原始事实': fact})
    if not rows or not components:
        raise ValueError('契约为空。')
    return {'格式版本': 1, '来源': 'SimC 实际编译用作用域契约',
            '复核源码提交': constant('skill_damage_scope_revision'),
            '复核客户端版本': constant('skill_damage_scope_build'),
            '复核摘要': constant('skill_damage_scope_sha256'),
            '契约内容摘要': hashlib.sha256(source.encode()).hexdigest(),
            '处理规则': '已标记为全局的天赋、增益、减益统一进入全局表，并从技能条件中排除；混合标记不构成豁免。',
            '接入状态': '已接入归一化导出器；导出时按局部技能绑定过滤混合状态。',
            '覆盖说明': '来源表覆盖现行契约中的全局效果；分量表同时保留局部分量。保留不等于每个分量都造成伤害，不能据此建立技能关联。',
            '全局效果表': rows, '分量判定表': components}


def render_markdown(data):
    lines = ['# 归一化全局效果数据表', '',
             f'复核客户端版本：`{data["复核客户端版本"]}`。', '', data['处理规则'], '',
             '**接入状态：**' + data['接入状态'], '', data['覆盖说明'], '',
             f'共 {len(data["全局效果表"])} 条职业、专精、来源记录；完整加成与原始事实见同名 JSON。', '',
             '| 职业 | 专精 | 类型 | 名称 | 法术 ID | 全局分量 | 局部分量 | 数据完整性 | 上表 | 下表条件 |',
             '| --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- |']
    for row in data['全局效果表']:
        values = [row[k] for k in ('职业', '专精', '类型', '名称')]
        values += [','.join(map(str, row['法术ID'])), len(row['全局分量']),
                   len(row['局部分量']), row['数据完整性'], '显示',
                   '排除全局分量；仅保留局部证据']
        lines.append('| ' + ' | '.join(str(v).replace('|', '\\|') for v in values) + ' |')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = export_tables(args.contract.read_text(encoding='utf-8'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.output.with_suffix('.md').write_text(render_markdown(data), encoding='utf-8')
    print(f'全局来源 {len(data["全局效果表"])} 条，效果分量 {len(data["分量判定表"])} 条。')
