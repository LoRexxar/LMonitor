"""行为证据分级；只使用已经校验的记录，不读取数据库或请求网络。"""
from collections import defaultdict


CONTEXT_UNKNOWNS = ['是否收费或存在交易', '是否朋友互助及组队动机', '钥匙归属与库存', '是否长期经营车队']
LEVELS = {
    'insufficient': ('证据不足', '低'),
    'limited': ('线索有限', '低'),
    'partial': ('部分支持', '中'),
    'strong': ('证据充分', '高'),
    'excluded': ('已排除（本轮样本）', '高'),
    'unverifiable': ('来源无法判定', '低'),
}
RULES = {
    'carrying': [
        '样本内已排除：至少三场且全部阵容可验证，没有符合重复轮换条件的四人组合，或每个候选组合均有跑前成绩反证，无法形成两名不同受益者；不代表该角色从未带人。',
        '证据不足：没有已核实的受益事件或有意义的待核验事件；固定队和普通换人本身不构成带人证据。',
        '线索有限：已有四人跑前能力与轮换关系，但第五人的可见成绩提升尚待核实；不发布4带1标签。',
        '部分支持：同一四人组合至少三场、存在不同第五人，但仅一名受益成员的跑前到跑后提升已核实；不发布4带1标签。',
        '证据充分：同一四人组合至少三场、至少两名不同第五人在独立场次有可见提升；每场四人均有跑前同本相当或更高限时成绩。',
    ],
    'other': [
        '线索有限：仅一个满足该行为条件的独立场次；不发布重复行为标签。',
        '部分支持：至少两个独立场次满足该行为各自的时序与成员条件。',
        '证据充分：至少三个独立场次，跨至少两个UTC日期，均满足该行为各自的时序与成员条件。',
        '钥匙实际归属与供给无法由成绩或阵容接口判定，单独标记来源无法判定，不发布确认标签。',
        '样本内排除要求至少三场且每场均有明确反向证据；缺失、空历史、隐私或过期记录不作为反证。',
        '相对成绩仅排除双方已核实的跑前比较，共同提高仅排除已发现且阵容完整的前场候选，不外推未知历史。',
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
    if not strong and inquiry.get('exclusion'):
        grades['carrying'] = _grade('excluded', verified_runs=0,
            assessed_runs=len(inquiry['exclusion']['run_ids']),
            scope='仅排除本轮已验证公开样本内的重复4带1模式，不代表该角色从未协助过他人。')
    for code, resolution in review.get('label_reviews', {}).items():
        if resolution['status'] in ('excluded', 'unverifiable'):
            grades[code] = _grade(resolution['status'], scope=resolution['scope'],
                assessed_runs=len(resolution.get('excluded_run_ids', [])),
                reason=resolution.get('reason', ''), verified_runs=0)
        elif code in ('fixed_team', 'rotating_core'):
            grades[code] = _grade('strong' if resolution['status'] == 'supported' and resolution['complete'] else
                'partial' if resolution['status'] == 'supported' else 'insufficient', scope=resolution['scope'],
                verified_runs=len(resolution['supported_run_ids']))
    return grades
