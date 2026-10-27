"""逐标签核验：支持、反证、未决分开计数；所有否定严格绑定可见比较样本。"""
from copy import deepcopy

from botend.services.player_observation_behavior import _date, _MARGIN


LABELS = {
    'carrying': '4带1协助上分模式', 'being_carried': '疑似获得队友协助',
    'joint_progress': '共同提高成绩线索', 'high_score_collaborator': '高成绩协作者线索',
    'key_access': '钥匙协调线索', 'fixed_team': '近期固定五人／偶尔替补',
    'rotating_core': '近期固定组合与轮换成员',
}
PURPOSES = {
    'verify_relative_score': 'scored_runs', 'verify_relative_history': 'scored_history',
    'verify_joint_history': 'scored_history', 'resilience_gap': 'scored_history',
}


def resolve_labels(*, review, subject, selected, teams, run_records, records, histories,
                   before, higher, snapshot, evidence):
    """只向有具体场次和预期结论变化的缺口提出问题，不将空历史当作反证。"""
    checks = {code: {} for code in ('being_carried', 'high_score_collaborator', 'joint_progress')}
    questions = {}
    events = {e['run_id']: e for e in review['resilience']['events']}
    complete = len(selected) >= 3 and all(rid in teams and subject in teams[rid] for rid in selected)

    def ask(code, kind, key, rid, did, purpose, reason, **extra):
        rec = run_records[rid]
        if kind == 'scored_history':
            history = histories.get(key, {}).get(str(did), {})
            stamp = _date(history.get('fetched_at'))
            # 已回答的部分历史保留未知；同轮不换副本继续寻找想要的答案。
            if stamp and stamp >= rec['completed']:
                return
        signature = (kind, key, did, purpose, extra.get('required_level'))
        question = questions.setdefault(signature, dict(kind=kind, member_key=key,
            dungeon_id=did, run_ids=[], label_codes=[], purpose=purpose, priority=70,
            reason=reason, expected_change='补齐后重新核验本标签的支持条件和反向证据；空记录或隐私限制仍为未知。', **extra))
        if rid not in question['run_ids']:
            question['run_ids'].append(rid)
        if code not in question['label_codes']:
            question['label_codes'].append(code)
        if extra.get('prior_run_ids'):
            question['prior_run_ids'] = sorted(set(question.get('prior_run_ids', [])) | set(extra['prior_run_ids']))

    def counter(code, rid, reason):
        eid = evidence(code + '_counter', rid, reason)
        checks[code][rid] = {'state': 'excluded', 'evidence_ids': [eid]}

    for rid in selected:
        for code in checks:
            checks[code][rid] = {'state': 'unknown', 'evidence_ids': []}
            if rid in review['witnesses'][code]:
                checks[code][rid] = {'state': 'supported', 'evidence_ids': [f'behavior:{code}:{rid}']}
        if rid not in teams or subject not in teams[rid]:
            continue
        rec, team = run_records[rid], teams[rid]
        event = events.get(rid, {})
        start = _date(event.get('started_at'))
        if not start:
            continue
        peers = sorted(team - {subject})
        stable = any(rid in g['run_ids'] for g in review['structure']['comparison_groups'])
        priors = {key: higher(key, rec, start) for key in team}
        own = snapshot(subject, rec)

        # 反向比较要求双方都有真实跑前记录，后来的低分快照不能否定过去。
        def dated_comparison(left, right):
            a, b = before(left, rec, start), before(right, rec, start)
            if not a or not b:
                return None
            a, b = max(a, key=lambda r: r['score']), max(b, key=lambda r: r['score'])
            return a, b

        for code, left_subject in (('high_score_collaborator', True), ('being_carried', False)):
            if checks[code][rid]['state'] == 'supported':
                continue
            reversed_pairs = []
            for peer in peers:
                pair = dated_comparison(subject, peer)
                if not pair:
                    continue
                a, b = pair
                # 部分历史的最高值不是上界；还需有效当前最高计分快照与跑前记录交叉核验。
                ceiling = snapshot(subject if left_subject else peer, rec)
                opposing = b if left_subject else a
                reversed_pair = (ceiling is not None and opposing['score'] + _MARGIN >= ceiling['score']
                                 and opposing['level'] >= ceiling['level'])
                if reversed_pair:
                    reversed_pairs.append((a['run_id'], b['run_id']))
            if len(reversed_pairs) >= 2:
                pairs = '、'.join(f'本人前场{a}/同伴前场{b}' for a, b in reversed_pairs)
                counter(code, rid, f'双方同本跑前记录与有效当前最高计分快照交叉核验，至少两名同伴不满足该标签要求的相对高低方向（{pairs}）。'
                        '仅排除这组已核实跑前比较，不排除未公开记录、后续变化或实际协助。')

        higher_peers = event.get('relative_comparisons', {}).get('higher_peers', [])
        # 已知本人较高且有重复合作，补同伴成绩；缺少跑前能力时先查明确的相对线索。
        if stable and priors[subject] and checks['high_score_collaborator'][rid]['state'] == 'unknown':
            for key in peers:
                if snapshot(key, rec) is None:
                    ask('high_score_collaborator', 'scored_runs', key, rid, rec['dungeon_id'],
                        'verify_relative_score', f'场次{rid}本人已有同本跑前较高成绩；核验同伴当前成绩及反向跑前记录。')
        if stable and own and checks['high_score_collaborator'][rid]['state'] == 'unknown' and not priors[subject]:
            known_lower = [key for key in peers if (snap := snapshot(key, rec))
                           and own['level'] > snap['level'] and own['score'] - snap['score'] > _MARGIN]
            if len(known_lower) >= 3:
                ask('high_score_collaborator', 'scored_history', subject, rid, rec['dungeon_id'],
                    'verify_relative_history', f'场次{rid}有三名当前较低同伴，核验本人较高记录是否发生在开打之前。')
        if checks['being_carried'][rid]['state'] == 'unknown':
            leaders = [key for key in peers if priors[key]]
            if len(leaders) >= 3 and own is None:
                ask('being_carried', 'scored_runs', subject, rid, rec['dungeon_id'],
                    'verify_relative_score', f'场次{rid}至少三名同伴已有跑前更高记录，核验本人当前成绩。')
            if own:
                for key in peers:
                    snap = snapshot(key, rec)
                    if snap and snap['level'] > own['level'] and snap['score'] - own['score'] > _MARGIN and not priors[key]:
                        ask('being_carried', 'scored_history', key, rid, rec['dungeon_id'],
                            'verify_relative_history', f'场次{rid}同伴当前较高；核验其成绩发生时间，避免用后来进步倒推。')
                    if priors[key] and key not in {p['leader_key'] for p in higher_peers}:
                        proof = event.get('members', {}).get(key, {})
                        # 仅补接近完整的坚韧证明；不为单个标签扫描全队八本。
                        covered = proof.get('per_dungeon', {})
                        if own['score'] < max(r['score'] for r in priors[key]) and len(covered) >= 6:
                            for did in review['resilience']['dungeon_ids']:
                                if str(did) not in covered:
                                    ask('being_carried', 'scored_history', key, rid, did, 'resilience_gap',
                                        f'场次{rid}该同伴跑前更高且保护已核实至少六本，仅补剩余具体副本。',
                                        required_level=rec['level'] + 1)

        if checks['joint_progress'][rid]['state'] != 'unknown':
            continue
        candidates = [r for r in before(subject, rec, start) if r['level'] < rec['level']
                      and rec['score'] - r['score'] > _MARGIN]
        rejected = []
        for prior in candidates:
            pid = prior['run_id']
            if pid in teams and pid in run_records and subject in teams[pid]:
                if len(team & teams[pid]) < 4:
                    rejected.append(pid)
                    continue
            shared = [key for key in team if pid in records.get(key, {})
                      and records[key][pid]['dungeon_id'] == rec['dungeon_id']
                      and all(records[key][pid][field] == prior[field] for field in ('level', 'completed', 'timed'))
                      and abs(records[key][pid]['score'] - prior['score']) <= _MARGIN]
            if len(shared) >= 2:
                for key in peers:
                    if key not in shared:
                        ask('joint_progress', 'scored_history', key, rid, rec['dungeon_id'],
                            'verify_joint_history', f'已知共同前场{pid}到场次{rid}同本升层，核验其余同伴是否也参加该前场。',
                            prior_run_ids=[pid])
        if candidates and len(rejected) == len(candidates):
            counter('joint_progress', rid, '本轮已发现的升层候选前场' + '、'.join(map(str, rejected))
                    + '均有完整阵容，且与本场重合不足四人，不符合共同提高条件。仅排除这些前场候选，公开历史仍可能遗漏其他前场。')

    resolutions = {}
    for code, rows in checks.items():
        supported = [rid for rid, row in rows.items() if row['state'] == 'supported']
        excluded = [rid for rid, row in rows.items() if row['state'] == 'excluded']
        unknown = [rid for rid, row in rows.items() if row['state'] == 'unknown']
        # 高置信否定要求整个本轮比较范围均有明确反证，未知场次不参与凑数。
        status = 'excluded' if complete and len(excluded) == len(selected) else (
            'supported' if len(supported) >= 2 else 'unknown')
        resolutions[code] = dict(status=status, scope=(
            '仅限本轮已发现的同本共同前场候选及其完整阵容，不排除未公开的其他共同前场。' if code == 'joint_progress' else
            '仅限本轮双方均有日期的可见跑前成绩与有效计分快照比较；不否定实际协助、未知历史或后续成绩变化。'),
            supported_run_ids=supported, excluded_run_ids=excluded, unknown_run_ids=unknown,
            support_ids=[eid for row in rows.values() if row['state'] == 'supported' for eid in row['evidence_ids']],
            counter_ids=[eid for row in rows.values() if row['state'] == 'excluded' for eid in row['evidence_ids']])
    resolutions['key_access'] = dict(status='unverifiable', scope='公开成绩与阵容不包含实际钥匙归属、库存或供给记录。',
        reason='成绩补采不能确认或排除钥匙实际供给；需要可验证的钥匙来源或人工核实材料。',
        supported_run_ids=[], excluded_run_ids=[], unknown_run_ids=sorted(selected), support_ids=[], counter_ids=[])
    for code, present in (('fixed_team', bool(set(review['structure']['codes']) & {'fixed_five', 'occasional_substitution'})
                          and 'rotating_core' not in review['structure']['codes']),
                         ('rotating_core', 'rotating_core' in review['structure']['codes'])):
        eid = 'behavior:' + code + '_structure'
        status = 'supported' if present else 'excluded' if complete else 'unknown'
        if present or complete:
            review['evidence'].append({'id': eid, 'run_url': '', 'text':
                f'本轮{len(selected)}场公开记录，已验证阵容{review["structure"]["roster_runs"]}场；'
                + LABELS[code] + ('的阵容条件成立。' if present else '的阵容条件不成立。')
                + '仅描述该样本的组队结构，不代表长期关系或协作动机。'})
        resolutions[code] = dict(status=status, scope='仅限本轮已验证阵容；未公开场次和长期关系未知。',
            supported_run_ids=sorted(rid for rid in selected if rid in teams and subject in teams[rid]) if present else [],
            excluded_run_ids=sorted(selected) if status == 'excluded' else [],
            unknown_run_ids=[rid for rid in selected if rid not in teams or subject not in teams[rid]],
            support_ids=[eid] if present else [], counter_ids=[eid] if status == 'excluded' else [], complete=complete)
    return resolutions, list(questions.values())


def render_label_reviews(evidence):
    """由已校验的冻结事实生成全部标签进度，不受模型四项菜单截断影响。"""
    review = evidence.get('metrics', {}).get('behavior_review', {})
    refs = {item['id']: item for item in evidence.get('evidence', [])}
    result = []
    for code, resolution in review.get('label_reviews', {}).items():
        if code not in LABELS:
            continue
        grade = review.get('evidence_strengths', {}).get(code, {})
        questions = [deepcopy(q) for q in review.get('questions', []) if code in q.get('label_codes', [])]
        if code in ('fixed_team', 'rotating_core') and resolution.get('unknown_run_ids'):
            questions.append({'kind': 'roster', 'purpose': 'verify_sample_structure',
                'run_ids': list(resolution['unknown_run_ids']), 'reason': '补齐本轮缺失的五人阵容。',
                'expected_change': '按完整样本重新核验固定五人与轮换结构；隐私或采集失败仍保留未知。'})
        result.append({'code': code, 'label': LABELS[code], **deepcopy(resolution),
            'strength': deepcopy(grade),
            'evidence': [deepcopy(refs[eid]) for eid in resolution.get('support_ids', []) if eid in refs],
            'counter_evidence': [deepcopy(refs[eid]) for eid in resolution.get('counter_ids', []) if eid in refs],
            'questions': questions})
    return result
