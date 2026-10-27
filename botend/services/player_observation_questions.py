"""由具体未决问题驱动取证；本模块不发送请求、不推断私人关系。"""
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
from itertools import combinations

from botend.services.player_observation_behavior import _date, _record, _reference, _score, _MARGIN


def roster_questions(*, subject, runs, visible, season, recent_ids=None):
    """先发现重复成绩，再用少量关键阵容区分固定五人和四人轮换。"""
    records = {}
    for rid, run in runs.items():
        dungeon = run.get('dungeon')
        completed = _date(run.get('completed_at'))
        score = _score(run.get('score'))
        duration = run.get('clear_time_ms')
        if (dungeon and completed and score is not None and _reference(run, season) == rid
                and type(duration) is int and duration > 0
                and type(run.get('num_keystone_upgrades')) is int and run['num_keystone_upgrades'] > 0):
            records[rid] = (dungeon, completed, completed-timedelta(milliseconds=duration), score)
    repeats = {}
    for rid, (dungeon, completed, start, score) in records.items():
        prior = [pid for pid, (did, when, _, points) in records.items()
                 if did == dungeon and when < start and points + _MARGIN >= score]
        if prior:
            repeats[rid] = prior
    structural = {}
    if recent_ids is not None:
        recent = sorted(set(recent_ids) & records.keys())
        if len(recent) >= 3:
            for rid in recent:
                if not visible.get(rid):
                    structural[rid] = {'id': f'roster_structure:{rid}', 'kind': 'roster', 'run_ids': [rid],
                        'purpose': 'verify_sample_structure', 'prior_run_ids': [], 'prior_level_advantage': 0,
                        'reason': f'核验本轮场次{rid}的五人阵容，区分固定、轮换与未知样本。',
                        'expected_change': '完整阵容可确认或排除样本内组队结构，并为其他行为标签提供成员身份。'}
            # 已发现的升层候选也核验前场阵容，不由成绩相似推定同队。
            for rid in recent:
                did, _, start, score = records[rid]
                for pid, (prior_did, completed, _, points) in records.items():
                    if (prior_did == did and completed < start and points + _MARGIN < score
                            and not visible.get(pid)):
                        structural.setdefault(pid, {'id': f'roster_joint:{pid}', 'kind': 'roster', 'run_ids': [pid],
                            'purpose': 'verify_joint_roster', 'prior_run_ids': [], 'prior_level_advantage': 0,
                            'reason': f'核验升层候选前场{pid}与场次{rid}是否有至少四名相同成员。',
                            'expected_change': '阵容重合可支持共同提高；明确不足四人可排除此候选。'})
    if len(repeats) < 2:
        return structural
    from botend.services.player_observation import _member_identity
    observed = []
    for rid in repeats:
        fact = visible.get(rid)
        if fact:
            team = frozenset(_member_identity(m) for m in fact['roster'])
            if len(team) == 5 and subject in team and None not in team:
                observed.append(team)
    # 三场相同阵容先保留固定五人解释，不因还有缺口继续补齐。
    if len(observed) >= 3 and len(set(observed)) == 1:
        return structural
    questions = {}
    for rid, priors in repeats.items():
        if visible.get(rid):
            continue
        level = runs[rid].get('mythic_level')
        higher = [pid for pid in priors if type(level) is int
                  and type(runs[pid].get('mythic_level')) is int
                  and runs[pid]['mythic_level'] > level]
        advantage = max((runs[pid]['mythic_level'] - level for pid in higher), default=0)
        questions[rid] = {
            'id': f'roster_relationship:{rid}', 'kind': 'roster', 'run_ids': [rid],
            'purpose': 'verify_repeated_relationship', 'prior_run_ids': sorted(priors),
            'prior_level_advantage': advantage,
            'reason': f'场次{rid}发生在本人同本相当或更高成绩之后；核验是否保留四人并更换第五人。',
            'selection_reason': ('已有跑前更高层限时记录，优先区分降层后的轮换协助。' if higher
                                 else '已有跑前同层相当成绩，核验重复通关的组队关系。'),
            'expected_change': '阵容可支持或否定轮换关系；单纯重复通关不发布带人标签。',
        }
    return {**structural, **questions}


def four_plus_one(*, subject, selected, teams, run_records, rosters, records, histories):
    """四人跑前能力与两个不同受益成员共同成立，才形成重复模式。"""
    groups = defaultdict(list)
    for rid in selected:
        if rid not in teams or not run_records[rid]['timed']:
            continue
        for core in combinations(sorted(teams[rid]), 4):
            if subject in core:
                groups[core].append(rid)
    questions, core_questions, candidates, supported = {}, {}, [], []
    closed_queries = set()
    checked_core_members = set()
    possible_groups = 0
    unresolved_groups = 0
    excluded_events = set()
    for core, ids in sorted(groups.items()):
        guests = {next(iter(teams[rid] - set(core))) for rid in ids}
        if len(ids) < 3 or len(guests) < 2:
            continue
        possible_groups += 1
        possible_guests = set()
        confirmed = {}
        pending = []
        group_core_questions = {}
        has_core_proof = False
        for rid in sorted(ids, key=lambda rid: (run_records[rid]['completed'], rid)):
            rec = run_records[rid]
            duration = rosters[rid][0].get('clear_time_ms')
            if type(duration) is not int or duration <= 0:
                possible_guests.add(next(iter(teams[rid] - set(core))))
                continue
            start = rec['completed'] - timedelta(milliseconds=duration)
            did = rec['dungeon_id']
            guest = next(iter(teams[rid] - set(core)))
            priors = {key: [r for r in records.get(key, {}).values()
                       if r['dungeon_id'] == did and r['completed'] < start]
                      for key in (*core, guest)}
            # 反证必须是实际跑前成绩；当前低分、缺失历史不能排除受益。
            counters = [r for r in priors[guest] if r['score'] + _MARGIN >= rec['score']]
            if counters:
                excluded_events.add((rid, max(counters, key=lambda r: r['score'])['run_id']))
                history = histories.get(guest, {}).get(str(did))
                if history and _date(history.get('fetched_at')) and _date(history['fetched_at']) >= rec['completed']:
                    closed_queries.add(guest)
                continue
            possible_guests.add(guest)
            # 四人均有跑前能力，不能拿本场或之后的成绩倒推。
            attained = {key: [r for r in priors[key] if r['timed'] and r['level'] >= rec['level']
                             and r['score'] + _MARGIN >= rec['score']] for key in core}
            if not all(attained.values()):
                for key in core:
                    history = histories.get(key, {}).get(str(did))
                    observed_core = bool(history and _date(history.get('fetched_at'))
                                         and _date(history['fetched_at']) >= rec['completed'])
                    if observed_core and not attained[key]:
                        checked_core_members.add(key)
                    if attained[key] or observed_core:
                        continue
                    group_core_questions.setdefault(key, {
                        'kind': 'scored_history', 'member_key': key, 'dungeon_id': did,
                        'run_ids': [rid], 'priority': 90, 'purpose': 'verify_core_history',
                        'core_members': list(core),
                        'reason': f'四人组合已重复至少三场并出现不同第五人；核验场次{rid}中该成员同本跑前能力。',
                        'expected_change': '找到跑前相当成绩则继续核验受益人；未查到不等于能力不足，保留未知。'})
                continue
            has_core_proof = True
            proof = {key: max(rows, key=lambda r: r['score'])['run_id'] for key, rows in attained.items()}
            history = histories.get(guest, {}).get(str(did))
            observed = bool(history and _date(history.get('fetched_at'))
                            and _date(history['fetched_at']) >= rec['completed'])
            if observed:
                # 接口返回的缺失记录不是零分；必须确有可见跑前记录及本场。
                contains_run = any(r.get('run_id') == rid for r in history.get('runs', []))
                if not priors[guest] or not contains_run or rid not in records.get(guest, {}):
                    closed_queries.add(guest)
                    continue
                previous = max(priors[guest], key=lambda r: r['score'])
                if rec['score'] - previous['score'] <= _MARGIN:
                    continue
                event = {'run_id': rid, 'guest': guest, 'core': list(core),
                         'prior_run_ids': proof, 'guest_prior_run_id': previous['run_id'],
                         'guest_prior_level': previous['level'], 'level': rec['level'],
                         'guest_prior_score': float(previous['score']), 'score': float(rec['score'])}
                confirmed.setdefault(guest, event)
            else:
                pending.append({'kind': 'scored_history', 'member_key': guest, 'dungeon_id': did,
                    'run_ids': [rid], 'priority': 100, 'purpose': 'verify_guest_gain',
                    'core_members': list(core), 'prior_run_ids': proof,
                    'reason': f'场次{rid}的四名成员已有同本相当或更高成绩，且该组合存在不同第五人；核验第五人的可见跑前成绩是否被本场提高。',
                    'expected_change': '确认可见提升则增加一个独立受益成员；已有相当成绩则排除此事件；没有跑前记录保持未知。'})
        candidates.extend(confirmed.values())
        if len(possible_guests) >= 2:
            unresolved_groups += 1
            if not has_core_proof:
                for key, question in group_core_questions.items():
                    core_questions.setdefault(key, question)
        if len(confirmed) >= 2:
            supported.extend(list(confirmed.values())[:2])
            continue
        pending = [q for q in pending if q['member_key'] not in confirmed and q['member_key'] not in closed_queries]
        if len(set(confirmed) | {q['member_key'] for q in pending}) < 2:
            continue  # 其余替补已有相当成绩时，补一个未知成员也不足以构成重复受益模式。
        # 同一第五人先验证最早一个有明确四人跑前证据的事件，避免连续刷同类问题。
        for q in pending:
            if q['member_key'] in confirmed:
                continue
            key = q['member_key']
            questions.setdefault(key, q)
    # 结论只覆盖本轮完整可见的样本，不外推角色整个赛季或私人身份。
    complete = len(selected) >= 3 and all(rid in teams and subject in teams[rid] for rid in selected)
    exclusion = None
    if complete and not unresolved_groups and not supported:
        exclusion = {'run_ids': sorted(selected), 'candidate_groups': possible_groups,
            'counter_records': sorted(excluded_events),
            'reason': ('本轮全部已验证阵容中，没有同时满足至少三场及两个不同第五人的四人组合。'
                       if not possible_groups else
                       '本轮每个候选四人组合中，除至多一名第五人外，其余第五人在对应场次之前均已有相当或更高成绩；不满足至少两名不同受益者。')}
    if supported:
        return {'events': supported, 'questions': [], 'candidates': candidates, 'exclusion': None}
    pending = [q for key, q in questions.items() if key not in closed_queries]
    if not pending and not closed_queries:
        pending = [q for key, q in core_questions.items() if key not in checked_core_members]
    return {'events': [], 'questions': [] if exclusion else pending,
            'candidates': candidates, 'exclusion': exclusion}


def observed_runs(activity, season):
    """沿已知同场成员的缓存引用扩展样本，不扫描全赛季阵容，不请求网络。"""
    from botend.models import RaiderIOCharacterActivity, RaiderIORunRoster
    from botend.services.player_observation import canonical_observation_runs, _member_identity
    from botend.services.player_observation_sources import validate_scored_runs
    from botend.services.player_observation_history import _state, scope
    from botend.services.raiderio_run_roster import validate_details
    from django.utils import timezone
    now = timezone.now()
    best, recent, runs, conflicts, invalid = deepcopy(canonical_observation_runs(activity, season))
    stamp = _date((activity.module_fetched_at or {}).get('mythic_plus_recent_runs') or activity.fetched_at)
    if not stamp or not roster_questions(subject=activity.identity_digest, runs=runs, visible={}, season=season):
        return best, recent, runs, conflicts, invalid
    members = {}
    for row in RaiderIORunRoster.objects.filter(rio_season=season, run_id__in=runs,
            roster_visible=True, status='ready', last_error='').only('run_id', 'payload_json'):
        try:
            clean = validate_details(row.payload_json, runs[row.run_id], season)
            keys = [(_member_identity(m), m['character'].get('id')) for m in clean['roster']]
            if sum(key == activity.identity_digest for key, _ in keys) == 1:
                for key, cid in keys:
                    if key and type(cid) is int and cid > 0:
                        members.setdefault(key, set()).add(cid)
        except (ValueError, KeyError, TypeError):
            continue
    members = {key: next(iter(ids)) for key, ids in members.items() if len(ids) == 1}
    candidates = defaultdict(list)
    profiles = defaultdict(list)
    for row in RaiderIOCharacterActivity.objects.filter(season_id=activity.season_id,
            identity_digest__in=sorted(members)[:40]).only(
                'identity_digest', 'payload_json', 'scored_runs_json', 'scored_runs_fetched_at',
                'module_fetched_at', 'fetched_at'):
        cid = members[row.identity_digest]
        values = []
        source_stamp = _date((row.module_fetched_at or {}).get('mythic_plus_recent_runs') or row.fetched_at)
        if source_stamp and source_stamp <= now:
            _, known_recent, _, _, _ = canonical_observation_runs(row, season)
            for profile in known_recent:
                rid, when = _reference(profile, season), _date(profile.get('completed_at'))
                if (rid not in conflicts and when and stamp-timedelta(days=14) <= when <= min(stamp, source_stamp)
                        and _score(profile.get('score')) is not None):
                    profiles[rid].append(profile)
        if row.scored_runs_fetched_at and row.scored_runs_fetched_at <= now:
            try:
                score = validate_scored_runs(row.scored_runs_json, season, cid)
                values += [r for d in score['dungeons'] for r in (d, d.get('alternate'))
                           if r and _date(r['completed_at']) <= row.scored_runs_fetched_at]
            except (ValueError, KeyError, TypeError):
                pass
        for did, history in row.payload_json.get('mythic_plus_scored_history', {}).items():
            if not isinstance(history, dict):
                continue
            asof = _date(history.get('fetched_at'))
            try:
                expected = scope(season, cid, int(did))
            except (ValueError, TypeError):
                continue
            if asof and asof <= now and _state(history, expected, asof) == 'fresh':
                values += [r for r in history['runs'] if _date(r['completed_at']) <= asof]
        for value in values:
            rec = _record(value)
            if rec and stamp-timedelta(days=14) <= rec['completed'] <= stamp and rec['run_id'] not in conflicts:
                candidates[rec['run_id']].append(rec)
    # 所有查询都使用已知场次编号走唯一索引，候选来自最多四十个已知成员。
    ids = sorted(candidates.keys() | profiles.keys(), key=lambda rid: (
        candidates[rid][0]['completed'] if candidates[rid] else _date(profiles[rid][0]['completed_at']), rid), reverse=True)[:400]
    recent_by_id = {_reference(r, season): r for r in recent}
    for row in RaiderIORunRoster.objects.filter(rio_season=season, run_id__in=ids,
            roster_visible=True, status='ready', last_error='').only('run_id', 'payload_json'):
        raw = row.payload_json
        records = candidates[row.run_id] + [rec for p in profiles[row.run_id]
            if (rec := _record(p, rid=row.run_id, dungeon=raw.get('dungeon', {}).get('id'), profile=True))]
        if not records:
            continue
        rec = records[0]
        if any(any(other[k] != rec[k] for k in ('dungeon_id', 'level', 'timed', 'completed'))
               or abs(other['score']-rec['score']) > _MARGIN for other in records[1:]):
            continue
        profile = runs.get(row.run_id) or {
            'url': f'https://raider.io/mythic-plus-runs/{season}/{row.run_id}',
            'dungeon': raw.get('dungeon', {}).get('name'), 'zone_id': rec['dungeon_id'],
            'mythic_level': rec['level'], 'completed_at': rec['completed'].isoformat(),
            'clear_time_ms': raw.get('clear_time_ms'), 'score': float(rec['score']),
            'num_keystone_upgrades': raw.get('num_chests')}
        try:
            clean = validate_details(raw, profile, season)
            if not clean or sum(_member_identity(m) == activity.identity_digest for m in clean['roster']) != 1:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        recent_by_id.setdefault(row.run_id, profile)
    recent = sorted(recent_by_id.values(), key=lambda r: _date(r['completed_at']), reverse=True)[:20]
    for r in recent:
        runs.setdefault(_reference(r, season), r)
    return best, recent, dict(sorted(runs.items())), conflicts, invalid
