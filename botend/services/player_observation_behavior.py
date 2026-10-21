"""对已验证记录作纯时序分析；近期与已有缓存最多二十场，不把样本当作完整历史。"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations
import re


_CODES = ('carrying', 'being_carried', 'joint_progress', 'key_access', 'high_score_collaborator')
_MARGIN = Decimal('0.2')


def _date(value):
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        return value if isinstance(value, datetime) and value.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _identity(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _positive(value):
    return type(value) is int and value > 0


def _score(value):
    if type(value) not in (int, float, Decimal):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def _reference(row, season):
    match = re.fullmatch(r'https://raider\.io/mythic-plus-runs/' + re.escape(season)
                         + r'/([1-9][0-9]*)(?:-[^/?#]*)?/?', row.get('url', ''))
    if not match:
        return None
    rid = int(match[1])
    return rid if row.get('keystone_run_id', rid) == rid else None


def _record(row, *, rid=None, dungeon=None, profile=False):
    """Normalize only white-listed scalar facts; malformed facts stay unknown."""
    if not isinstance(row, dict):
        return None
    rid = rid or row.get('run_id', row.get('keystone_run_id'))
    dungeon = dungeon or row.get('dungeon_id')
    if dungeon is None and isinstance(row.get('dungeon'), dict):
        dungeon = row['dungeon'].get('id')
    level = row.get('level', row.get('mythic_level'))
    completed = _date(row.get('completed_at'))
    points = _score(row.get('score'))
    timed = row.get('timed')
    if type(timed) is not bool:
        chests = row.get('num_keystone_upgrades' if profile else 'num_chests')
        timed = chests > 0 if type(chests) is int and chests >= 0 else None
    if not all(_positive(v) for v in (rid, dungeon, level)) or not completed or points is None or timed is None:
        return None
    return dict(run_id=rid, dungeon_id=dungeon, level=level, score=points, timed=timed, completed=completed)


def build_behavior_review(*, subject, best, recent, runs, rosters, scores,
                          score_times, fetched_at, rio_season, now,
                          dungeon_ids=None, histories=None):
    """返回可序列化的行为证据，不修改输入或发送请求。

    supported 只包含通过对应行为条件的标签；4带1另外要求重复四人组合与
    两名独立受益成员。witnesses 保留单场线索，evidence_strengths 单独分级。
    历史身份由采集层认证，本层再次核对范围。保护证据逐成员、逐副本绑定
    有日期的场次，未核实则保持未知；成绩达标不代表实际钥匙库存。
    """
    structure = dict(recent_runs=0, roster_runs=0, untimed_runs=0, codes=[],
                     lineups=[], comparison_groups=[], observed_config_switches=0)
    out = dict(version=1, triggered=False, structure=structure, evidence=[], supported={},
               witnesses={code: [] for code in _CODES}, resilience=dict(subject_key=subject, events=[]), questions=[],
               limitations=[
                   '仅近期公开完成记录及中央已有阵容，最多20场、14天内；不是完整历史。',
                   '行为证据强度与动机、钥匙主人、背包库存或收费分别判断；朋友互助不是带人反证。',
                   '全L保护仍可能需要从L升至L+1；全L+1只削弱自然掉至L的解释，不排除旧钥匙或他人钥匙。',
                   '当前成绩不回填过去；未观测记录不是零分或未达成，后续低分快照不作硬否定。'])
    now, fetched = _date(now), _date(fetched_at)
    if not _identity(subject) or not now or not fetched or not now-timedelta(hours=48) <= fetched <= now:
        out['limitations'].append('身份或近期快照无效/过期，未评估行为。')
        return out
    # Caller supplies the verified season universe; do not infer it from records.
    universe = set(dungeon_ids) if isinstance(dungeon_ids, (set, frozenset, list, tuple)) else set()
    known_scope = len(universe) == 8 and all(_positive(x) for x in universe) and rio_season == 'season-mn-2'
    out['resilience'].update(dungeon_ids=sorted(universe) if all(_positive(x) for x in universe) else [],
        scope_verified=known_scope, baseline_season='season-mn-2' if known_scope else None)

    records = defaultdict(dict)
    invalid = set()
    snapshot_rows = defaultdict(list)

    def add(key, rec, asof):
        if not _identity(key) or not rec or not asof or rec['completed'] > asof or asof > now:
            return
        identity = (key, rec['run_id'])
        if identity in invalid:
            return
        old = records[key].get(rec['run_id'])
        if old and (any(old[x] != rec[x] for x in ('dungeon_id', 'level', 'timed', 'completed'))
                    or abs(old['score']-rec['score']) > Decimal('0.15')):
            invalid.add(identity)
            records[key].pop(rec['run_id'], None)
        elif not old:
            records[key][rec['run_id']] = rec

    for key, rows in scores.items():
        asof = _date(score_times.get(key))
        for row in rows:
            for item in (row, row.get('alternate')):
                rec = _record(item)
                add(key, rec, asof)
                if rec and asof and rec['completed'] <= asof <= now:
                    snapshot_rows[key].append(rec)

    accepted_histories = defaultdict(dict)
    for key, dungeons in (histories or {}).items():
        for did, history in dungeons.items():
            if not isinstance(history, dict):
                continue
            ui = history.get('ui', {})
            asof = _date(history.get('fetched_at'))
            if (not isinstance(ui, dict) or ui.get('season') != rio_season
                    or not _positive(ui.get('characterId')) or str(ui.get('dungeonId')) != str(did)
                    or any(ui.get(k) != v for k, v in dict(role='all', mode='scored', affixes='all', date='all').items())
                    or not isinstance(history.get('source_url'), str) or not history['source_url'].startswith('https://raider.io/')
                    or history.get('private') is True
                    or history.get('status') in {'private', 'error', 'fetching'}
                    or history.get('last_error')
                    or not asof or asof > now):
                continue
            accepted_histories[key][str(ui['dungeonId'])] = history
            for row in history.get('runs', []):
                rec = _record(row)
                if rec and rec['dungeon_id'] == ui['dungeonId']:
                    add(key, rec, asof)

    teams, run_records = {}, {}
    for rid, value in rosters.items():
        clean, keyed = value
        team = frozenset(key for key, _ in keyed)
        if len(keyed) != 5 or len(team) != 5 or not all(_identity(key) for key in team):
            continue
        row = runs.get(rid)
        if not isinstance(row, dict) or _reference(row, rio_season) != rid:
            continue
        rec = _record(row, rid=rid, dungeon=clean.get('dungeon', {}).get('id'), profile=True)
        if not rec or rec['completed'] > fetched:
            continue
        if (clean.get('mythic_level') != rec['level']
                or (clean.get('completed_at') and _date(clean['completed_at']) != rec['completed'])
                or ('num_chests' in clean and (clean['num_chests'] > 0) != rec['timed'])):
            continue
        teams[rid], run_records[rid] = team, rec
        for key in team:
            add(key, rec, fetched)
    for row in best:
        rid = _reference(row, rio_season)
        dungeon = row.get('zone_id')
        if rid in rosters:
            dungeon = rosters[rid][0].get('dungeon', {}).get('id')
        add(subject, _record(row, rid=rid, dungeon=dungeon, profile=True), fetched)

    selected = {}
    for row in recent[:20]:
        rid = _reference(row, rio_season)
        completed = _date(row.get('completed_at'))
        if (rid not in runs or not completed or not now-timedelta(days=14) <= completed <= fetched
                or not _positive(row.get('mythic_level'))
                or type(row.get('num_keystone_upgrades')) is not int or row['num_keystone_upgrades'] < 0):
            continue
        selected[rid] = row
    ordered = sorted(selected, key=lambda rid: (_date(selected[rid]['completed_at']), rid))
    visible = [rid for rid in ordered if rid in teams and subject in teams[rid]]
    structure.update(recent_runs=len(selected), roster_runs=len(visible),
                     untimed_runs=sum(row['num_keystone_upgrades'] == 0 for row in selected.values()))
    counts = Counter(tuple(sorted(teams[rid])) for rid in visible)
    structure['lineups'] = [dict(members=list(team), count=count,
        run_ids=[rid for rid in visible if tuple(sorted(teams[rid])) == team])
        for team, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    structure['observed_config_switches'] = sum(teams[a] != teams[b] for a, b in zip(visible, visible[1:]))
    if counts and max(counts.values()) >= 3 and max(counts.values())*5 >= len(selected)*4:
        structure['codes'].append('fixed_five')
    groups = defaultdict(list)
    for rid in visible:
        for size in (3, 4):
            for members in combinations(sorted(teams[rid]), size):
                groups[members].append(rid)
    for group, ids in sorted(groups.items(), key=lambda item: (-len(item[1]), -len(item[0]), item[0])):
        if len(ids) < 2:
            continue
        configs = [tuple(sorted(teams[rid]-set(group))) for rid in ids]
        non_dominant = len(ids)-max(Counter(configs).values())
        switches = sum(a != b for a, b in zip(configs, configs[1:]))
        structure['comparison_groups'].append(dict(members=list(group), run_ids=ids,
            includes_subject=subject in group, non_dominant_appearances=non_dominant,
            observed_config_switches=switches))
        if len(ids) >= 3:
            code = ('occasional_substitution' if non_dominant == 1 else
                    'rotating_core' if non_dominant >= 2 and switches >= 2 else
                    'core_member_change' if non_dominant >= 2 and switches == 1 else None)
            if code and code not in structure['codes']:
                structure['codes'].append(code)

    # Connect complete validated records BEFORE selecting a best record. Conflicts
    # in a roster never erase independent authenticated personal score sources.
    shared = defaultdict(dict)
    for key, rows in records.items():
        for rec in rows.values():
            signature = (rec['run_id'], rec['dungeon_id'], rec['level'], rec['completed'], rec['timed'])
            shared[signature][key] = rec

    def before(key, rec, start):
        return [r for r in records[key].values() if start and r['completed'] < start
                and r['dungeon_id'] == rec['dungeon_id'] and r['run_id'] != rec['run_id']]

    def higher(key, rec, start):
        return [r for r in before(key, rec, start) if r['timed'] and r['level'] > rec['level']
                and r['score']-rec['score'] > _MARGIN]

    def protection(key, level, start):
        proof = {}
        if known_scope and start and 12 <= level <= 30:
            for did in sorted(universe):
                options = [r for r in records[key].values() if r['dungeon_id'] == did
                           and r['timed'] and r['level'] >= level and r['completed'] < start]
                if options:
                    r = min(options, key=lambda r: (r['completed'], r['run_id']))
                    proof[str(did)] = dict(run_id=r['run_id'], completed_at=r['completed'].isoformat(), level=r['level'])
        confirmed = known_scope and len(proof) == 8
        return dict(state='confirmed_before_start' if confirmed else 'unknown', required_level=level,
                    per_dungeon=proof, necessary_score_baseline=365+15*(level-12) if known_scope and 12 <= level <= 30 else None)

    def snapshot(key, rec):
        stamp = _date(score_times.get(key))
        if not stamp or not now-timedelta(hours=48) <= stamp <= now:
            return None
        rows = [r for r in snapshot_rows[key] if r['dungeon_id'] == rec['dungeon_id']
                and (key, r['run_id']) not in invalid]
        if not rows:
            return None
        candidate = max(rows, key=lambda r: (r['score'], r['level']))
        # A later observation may supersede a snapshot; a contradiction before
        # its timestamp invalidates it. Neither kind is a current-score proxy.
        if any(r['score']-candidate['score'] > _MARGIN for r in records[key].values()
               if r['dungeon_id'] == rec['dungeon_id']):
            return None
        return candidate

    evidence_ids = defaultdict(list)

    def relative_lower(peer, leader_prior, rec, start):
        """Compare a dated score proxy with a verified pre-start higher run.

        The snapshot may be this run itself; it is never a historical score
        delta. An isolated lower old completion cannot prove a personal ceiling.
        """
        snap = snapshot(peer, rec)
        if not start or not snap:
            return None
        leaders = [r for r in leader_prior if r['completed'] < start
                   and r['dungeon_id'] == rec['dungeon_id'] and r['timed']
                   and r['level'] > rec['level'] and r['level'] > snap['level']
                   and r['score']-snap['score'] > _MARGIN]
        if not leaders:
            return None
        leader = max(leaders, key=lambda r: (r['score'], r['level']))

        def fact(row):
            return dict(run_id=row['run_id'], completed_at=row['completed'].isoformat(),
                        level=row['level'], score=float(row['score']),
                        run_url=f"https://raider.io/mythic-plus-runs/{rio_season}/{row['run_id']}")

        return dict(member_key=peer, snapshot=fact(snap),
                    snapshot_as_of=_date(score_times[peer]).isoformat(),
                    snapshot_source='scored_runs', leader_prior=fact(leader))

    def evidence(kind, rid, text):
        eid = f'behavior:{kind}:{rid}'
        if not any(e['id'] == eid for e in out['evidence']):
            out['evidence'].append(dict(id=eid, text=text, run_url=selected[rid]['url']))
        return eid

    def witness(code, rid, text):
        if rid not in out['witnesses'][code]:
            out['witnesses'][code].append(rid)
            evidence_ids[code].append(evidence(code, rid, text+' 仅说明所引场次的行为，不证明动机或交易。'))

    for rid in visible:
        rec, team = run_records[rid], teams[rid]
        clean = rosters[rid][0]
        duration = clean.get('clear_time_ms')
        start = (rec['completed']-timedelta(milliseconds=duration)
                 if _positive(duration) and _date(clean.get('completed_at')) == rec['completed'] else None)
        states = {key: protection(key, rec['level']+1, start) for key in sorted(team)}
        event = dict(run_id=rid, level=rec['level'],
            started_at=start.isoformat() if start else None, members=states)
        out['resilience']['events'].append(event)
        priors = {key: higher(key, rec, start) for key in team}
        if priors[subject]:
            prior = max(priors[subject], key=lambda row: (row['score'], row['level'], -row['run_id']))
            evidence('prior_higher', rid,
                f"本场开打时间{start.isoformat()}之前，本人已于{prior['completed'].isoformat()}"
                f"完成同本{prior['level']}层限时（前场{prior['run_id']}）；"
                '不能以本场之后才达到该更高层解释这场记录；这不证明本场净增分、动机或坚韧资格。')
        observed = {key: [r for r in records[key].values() if r['dungeon_id'] == rec['dungeon_id']
                          and r['timed'] and r['level'] > rec['level']] for key in team}
        stable = any(rid in g['run_ids'] for g in structure['comparison_groups'])
        clue = bool(observed[subject]) and stable
        if clue:
            out['triggered'] = True
            evidence('lead', rid, f"本人已有同本较高层观测；本场{rec['level']}层，稳定合作仍需区分协助、升钥与后续进步。")

        lower_peers = [comparison for key in sorted(team-{subject})
                       if (comparison := relative_lower(key, priors[subject], rec, start))]
        protected_peers = [key for key in sorted(team-{subject}) if priors[key]
                           and states[key]['state'] == 'confirmed_before_start']
        higher_peers = [dict(comparison, leader_key=key) for key in protected_peers
                        if (comparison := relative_lower(subject, priors[key], rec, start))]
        event['relative_comparisons'] = dict(lower_peers=lower_peers, higher_peers=higher_peers)
        if priors[subject]:
            if states[subject]['state'] == 'confirmed_before_start':
                text = (f"本人此前同本更高，且全{rec['level']+1}跑前保护已证实；受益关系由独立事件核验；"
                        '这削弱本人钥匙自然降至本层的解释，仍不排除旧库存或他人钥匙，钥匙归属与供给未知。')
            else:
                text = (f"本人此前同本更高，但全{rec['level']+1}跑前保护未知（未完整证实）；"
                        '保留升钥/库存解释。')
            evidence('key_recovery_unknown', rid, text)
        if len(higher_peers) >= 3:
            witness('being_carried', rid, '本人有效dated计分快照低于至少三名同场同伴的跑前更高同本限时，且这些同伴全更高层保护达标；不是历史净增分。')
        if priors[subject] and len(lower_peers) >= 3 and stable:
            witness('high_score_collaborator', rid, '本人是已有较高同本记录的合作成员，至少三名同伴存在较低同本记录；不确定社会核心或外援身份。')
            # This is explicitly a competing explanation, not proof of a key.
            witness('key_access', rid, '较高成绩本人与较低同本成员稳定合作，可保留协调钥匙的竞争解释；钥匙归属与实际供给未知。')
        if not start:
            continue
        for signature, members in shared.items():
            prior_id, did, level, completed, _ = signature
            common = team & members.keys()
            if (did != rec['dungeon_id'] or completed >= start or level >= rec['level']
                    or subject not in common or len(common) < 4):
                continue
            values = [members[key]['score'] for key in common]
            if max(values)-min(values) > Decimal('0.15') or rec['score']-max(values) <= _MARGIN:
                continue
            no_gain = sorted(key for key in common if any(r['score']-rec['score'] > _MARGIN
                              for r in before(key, rec, start)))
            witness('joint_progress', rid, f'包含本人及至少三名同伴共同前场{prior_id}→本场同本升层；'
                f'共同成员{len(common)}人，已知无提升者{len(no_gain)}人，仍保留在比较中。仅为共同记录提高，不称全员实际净增分。')
            break
    for code in _CODES:
        out['witnesses'][code].sort()
        if len(out['witnesses'][code]) >= 2:
            out['supported'][code] = evidence_ids[code]
    # 取证只围绕四人协作与具体受益事件，不补全队八本资料。
    from botend.services.player_observation_questions import four_plus_one
    inquiry = four_plus_one(subject=subject, selected=selected, teams=teams,
        run_records=run_records, rosters=rosters, records=records, histories=accepted_histories)
    from botend.services.player_observation_strength import grade_review, CONTEXT_UNKNOWNS, RULES
    # 分级只消费本轮已有事实，不触发新问题或新抓取。
    structure['recent_run_ids'] = sorted(selected)
    out['evidence_strengths'] = grade_review(out, inquiry, teams, run_records)
    out['strength_rules'] = RULES
    out['context_unknowns'] = list(CONTEXT_UNKNOWNS)
    out['questions'] = inquiry['questions']
    for event in inquiry['events']:
        rid = event['run_id']
        guest_name = next(member.get('character', {}).get('name', '该第五人')
                          for key, member in rosters[rid][1] if key == event['guest'])
        witness('carrying', rid,
            f"同一四人组合反复与不同第五人通关；四人的跑前同本证据场次为{','.join(str(x) for x in sorted(set(event['prior_run_ids'].values())))}；"
            f"第五人{guest_name}的可见成绩由前场{event['guest_prior_run_id']}（{event['guest_prior_level']}层、{event['guest_prior_score']:g}分）"
            f"提高至本场（{event['level']}层、{event['score']:g}分）。"
            '已核验至少两名不同受益成员，未将一个成员的连续通关重复计数；公开历史可能不完整。')
    if inquiry['events']:
        out['supported']['carrying'] = evidence_ids['carrying']
    out['triggered'] = bool(out['triggered'] or inquiry['questions'])
    out['triggered'] = bool(out['triggered'] or any(out['witnesses'].values()))
    if invalid:
        out['limitations'].append('冲突的角色/场次成绩已隔离，不作时序证据。')
    return out
