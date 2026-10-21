"""行为证据分级；只使用已经校验的记录，不读取数据库或请求网络。"""
from collections import defaultdict


CONTEXT_UNKNOWNS = ['是否收费或存在交易', '是否朋友互助及组队动机', '钥匙归属与库存', '是否长期经营车队']
LEVELS = {
    'insufficient': ('证据不足', '低'),
    'limited': ('线索有限', '低'),
    'partial': ('部分支持', '中'),
    'strong': ('证据充分', '高'),
}
RULES = {
    'carrying': [
        '证据不足：没有已核实的受益事件或有意义的待核验事件；固定队和普通换人本身不构成带人证据。',
        '线索有限：已有四人跑前能力与轮换关系，但第五人的可见成绩提升尚待核实；不发布4带1标签。',
        '部分支持：同一四人组合至少三场、存在不同第五人，但仅一名受益成员的跑前到跑后提升已核实；不发布4带1标签。',
        '证据充分：同一四人组合至少三场、至少两名不同第五人在独立场次有可见提升；每场四人均有跑前同本相当或更高限时成绩。',
    ],
    'other': [
        '线索有限：仅一个满足该行为条件的独立场次；不发布重复行为标签。',
        '部分支持：至少两个独立场次满足该行为各自的时序与成员条件。',
        '证据充分：至少三个独立场次，跨至少两个UTC日期，均满足该行为各自的时序与成员条件。',
        '钥匙协调仅是相容解释，缺少实际钥匙归属或供给事实时保持线索有限。',
    ],
}


def _grade(level, **counts):
    label, confidence = LEVELS[level]
    return {'level': level, 'label': label, 'confidence': confidence,
            'scope': '仅限已观测的近期公开记录，不外推长期身份。', **counts}


def grade_review(review, inquiry, teams, run_records):
    """计数去重，受益人数按角色去重；未知动机不参与证据分数。"""
    grades = {}
    for code, ids in review['witnesses'].items():
        if code == 'carrying':
            continue
        ids = set(ids)
        days = {run_records[rid]['completed'].date() for rid in ids if rid in run_records}
        level = ('strong' if len(ids) >= 3 and len(days) >= 2 else
                 'partial' if len(ids) >= 2 else 'limited' if ids else 'insufficient')
        if code == 'key_access' and ids:
            level = 'limited'
        grades[code] = _grade(level, verified_runs=len(ids), observed_days=len(days))
    groups = defaultdict(list)
    for event in inquiry['candidates']:
        groups[tuple(event['core'])].append(event)
    best = max(groups.values(), key=lambda events: len({e['guest'] for e in events}), default=[])
    core = set(best[0]['core']) if best else set()
    beneficiaries = {e['guest'] for e in best}
    recurring = {rid for rid, team in teams.items()
                 if core and core < team and run_records[rid]['timed'] and rid in review['structure'].get('recent_run_ids', [])}
    # events 已通过至少三场及两个不同受益成员的规则；不把一个人的多场算多人。
    strong = (inquiry['events'] and len(core) == 4 and len(recurring) >= 3
              and len(beneficiaries) >= 2 and len({e['run_id'] for e in best}) >= 2)
    level = ('strong' if strong else 'partial' if best else
             'limited' if inquiry['questions'] else 'insufficient')
    grades['carrying'] = _grade(level, verified_runs=len({e['run_id'] for e in best}),
                                distinct_beneficiaries=len(beneficiaries), core_runs=len(recurring))
    return grades
