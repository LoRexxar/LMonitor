"""Pure, bounded inquiry planning from already validated central facts.

No ORM, transport, AI or writes. Recent samples alone supply frequencies; best
runs are only an explicitly named current-score proxy for the subject. Questions
are a plan, not permission to fetch. Unknown scores never become counterevidence.
"""
import itertools
import re
from collections import defaultdict

from botend.services.player_observation_progression import (
    RECENT_WINDOW, SNAPSHOT_TTL, _date, current_record,
)
from botend.services.raiderio_run_roster import run_reference

MIN_REPEAT = 3
LEVEL_GAP = 1
DIRECTION_GAP = 2
MAX_QUESTIONS = 8
HYPOTHESES = (
    ('routine_group', '日常协作、朋友互助或练习'),
    ('assistance', '稳定成员协助较低当前成绩成员'),
    ('key_access', '为特定副本或层数协调钥匙的可能性'),
    ('external_assistance', '较高当前成绩成员作为外援或朋友协作'),
    ('later_progress', '后续进步造成当前上限与近期通关差距'),
)


def _identity(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _fresh(value, now, completed=None):
    stamp = _date(value)
    return bool(stamp and max(now-SNAPSHOT_TTL, completed or now-SNAPSHOT_TTL) <= stamp <= now)


def build_investigation(*, subject, best, recent, runs, rosters, scores,
                        score_times, fetched_at, rio_season, now):
    """Return a v1 inquiry (including a non-triggered result), never None.

    subject is the canonical identity digest. rosters maps run ID to validated
    (clean, [(identity_digest, member), ...]); scores maps digest to validated
    scored-runs dungeons. fetched_at certifies the recent/profile snapshot;
    callers with separately timestamped best data must exclude stale best rows.
    Returned question priority is descending (100 peripheral, 50 core), and
    run_ids always refer to the selected recent seed, not unrelated history.
    """
    result = dict(version=1, triggered=False, trigger_reason='no_recent_signal',
        scope='最近至多10条公开完成记录、14天内；包含未限时，不代表完整历史。',
        seed_run_ids=[], core_members=[], comparison_groups=[], evidence=[], hypotheses=[], questions=[],
        stop_reason='no_recent_signal')
    stamp = _date(fetched_at)
    if not _identity(subject) or not _fresh(stamp, now):
        result.update(trigger_reason='invalid_identity_or_stale_source', stop_reason='invalid_identity_or_stale_source')
        return result

    def record(key, dungeon, completed):
        if not _fresh(score_times.get(key), now, completed):
            return None
        return current_record(scores.get(key, []), dungeon)[0]

    def own_record(run, clean, completed):
        own = record(subject, clean['dungeon'], completed)
        if own:
            return own, '当前计分缓存（限时记录）'
        proxies = []
        for candidate in best[:8]:
            try:
                bid = run_reference(candidate, rio_season)
            except (ValueError, TypeError):
                continue
            when = _date(candidate.get('completed_at'))
            if (bid in runs and candidate.get('dungeon') == run.get('dungeon')
                    and when and when <= stamp
                    and type(candidate.get('mythic_level')) is int
                    and type(candidate.get('num_keystone_upgrades')) is int
                    and candidate['num_keystone_upgrades'] > 0):
                proxies.append(candidate)
        proxy = max(proxies, key=lambda row: row['mythic_level']) if proxies else None
        return proxy, 'profile best 当前成绩代理（非完整计分缓存）'

    selected = {}
    for run in recent[:10]:
        try:
            rid = run_reference(run, rio_season)
        except (ValueError, TypeError):
            continue
        completed = _date(run.get('completed_at'))
        if (rid not in runs or rid not in rosters or not completed
                or not now-RECENT_WINDOW <= completed <= stamp
                or type(run.get('mythic_level')) is not int or run['mythic_level'] <= 0
                or type(run.get('num_keystone_upgrades')) is not int or run['num_keystone_upgrades'] < 0):
            continue
        clean, keyed = rosters[rid]
        team = frozenset(key for key, _ in keyed)
        if len(keyed) != 5 or len(team) != 5 or subject not in team or not all(_identity(key) for key in team):
            continue
        own, source = own_record(run, clean, completed)
        if own and own['mythic_level'] - run['mythic_level'] >= LEVEL_GAP:
            selected[rid] = (run, clean, team, completed, own, source)
    if len(selected) < MIN_REPEAT:
        return result
    supports = defaultdict(set)
    for rid, (_, _, team, _, _, _) in selected.items():
        for size in (3, 4, 5):
            for members in itertools.combinations(sorted(team), size):
                supports[frozenset(members)].add(rid)
    # Maximal co-occurrence describes the observed structure, not who is the
    # social core: subject-conditioned samples necessarily include the subject.
    candidates = [(core, ids) for core, ids in supports.items() if len(ids) >= MIN_REPEAT
        and not any(core < larger and ids == other_ids for larger, other_ids in supports.items())]
    if not candidates:
        return result
    core, seeds = max(candidates, key=lambda item: (len(item[1]), len(item[0]), tuple(sorted(item[0]))))
    # Keep equal-support competing subgroups separately instead of deleting
    # every possible core without the subject. They are comparisons, NOT proof
    # of a rotating seat or a true four/three-person core in a fixed group.
    groups = ([core] if len(core) < 5 else []) + sorted(
        (group for group, ids in supports.items()
         if group < core and len(group) == len(core)-1 and ids == seeds),
        key=lambda group: tuple(sorted(group)))
    result['comparison_groups'] = [dict(core_members=sorted(group), run_ids=sorted(seeds),
        kind='observed_periphery' if group == core else 'competing_group') for group in groups]
    result.update(triggered=True, trigger_reason='repeated_below_own_current_with_stable_core',
        seed_run_ids=sorted(seeds), core_members=sorted(core), stop_reason='evidence_sufficient_for_comparison')
    result['scope'] += (f'其中{len(seeds)}场本人低于自己的当前同本限时层数，'
        f'同一{len(core)}人稳定组合共同出现；' + ('固定五人也保留解释探索。' if len(core) == 5 else '外围席位有变化。')
        + '稳定组合不等于真实核心；保留竞争分组，不能确定其中谁是核心或外援。'
        + '当前成绩不是历史当场加分；所有动机仅为候选解释，不能确认钥匙归属或交易。')
    evidence = result['evidence']
    own_ids, structure_ids, low, high, counter, unknown, motive_ids = [], [], [], [], [], [], []
    external_counter = []
    missing = defaultdict(set)
    perimeter = set()

    def add(kind, rid, text):
        eid = f'investigation:{kind}:{rid}'
        evidence.append({'id': eid, 'text': text, 'run_url': selected[rid][0]['url']})
        return eid

    for rid in sorted(seeds):
        run, clean, team, completed, own, source = selected[rid]
        own_ids.append(add('own', rid,
            f"本场{run['mythic_level']}层（{'限时' if run['num_keystone_upgrades'] > 0 else '完成未限时'}），"
            f"低于本人{source}的{own['mythic_level']}层；通关时间{completed.isoformat()}。不是历史加分或动机证据。"))
        structure_ids.append(add('team', rid, f'已验证本场五人身份，所选{len(core)}人稳定组合均在场；频率只计独立近期通关，不确定真实核心身份。'))
        records = {key: record(key, clean['dungeon'], completed) for key in team}
        records[subject] = own
        for key in team:
            if records[key] is None:
                missing[key].add(rid)
        perimeter.update(team-core)
        for index, group in enumerate(groups):
            observed = group == core
            references = [records[key] for key in group if records[key] is not None]
            directions = []
            # Alternative groups compare stable members with one another;
            # rotating seats must not mask a stronger stable collaborator.
            outside = team-group if observed else core-group
            for key in sorted(outside):
                peer = records[key]
                if not peer or len(references) != len(group):
                    directions.append('unknown')
                    continue
                gaps = [peer['mythic_level'] - ref['mythic_level'] for ref in references]
                directions.append('high' if min(gaps) >= DIRECTION_GAP else 'low' if max(gaps) <= -DIRECTION_GAP
                    else 'near' if all(abs(gap) < DIRECTION_GAP for gap in gaps) else 'mixed')
            direction = ('unknown' if 'unknown' in directions else directions[0]
                if len(set(directions)) == 1 else 'mixed')
            words = {'high': '明显高于', 'low': '明显低于', 'near': '近似于', 'mixed': '高低混合于', 'unknown': '未知，不能判为反证或零分'}
            kind = direction if observed else f'{direction}:group{index}'
            eid = add(kind, rid, f"{'外围成员' if observed else f'竞争分组{index+1}的组外稳定成员'}当前同本限时层数方向：{words[direction]}"
                + (f'该{len(group)}人成绩对照组（明显差距至少{DIRECTION_GAP}层）。' if direction != 'unknown' else '。')
                + ('此分组仅为成员对照，不证明真实核心或外围变化。' if not observed else '')
                + '高成绩只提示外援、朋友等替代解释，不证明钥匙归属；低成绩也不证明交易。')
            if direction == 'low':
                low.append(eid)
                if observed:
                    external_counter.append(eid)
            elif direction == 'high':
                high.append(eid)
                if observed:
                    counter.append(eid)
            elif observed and direction != 'unknown':
                counter.append(eid)
            else:
                unknown.append(eid)
        if len(groups) > 1:
            unknown.append(add('group_unknown', rid, '竞争分组不能确定真实核心或成员角色；一种分组的成绩方向不能反证另一种协作解释。'))
        if any(records[key] is None for key in core):
            unknown.append(add('core_unknown', rid, '部分核心当前同本成绩缺失，无法确认全核心均在低于各自上限的层数活动；本人线索仍成立。'))
        motive_ids.append(add('motive_unknown', rid, '公开通关与当前计分没有历史加分、钥匙归属、组队目的或交易事实。'))
    low_runs = {eid.rsplit(':', 1)[-1] for eid in low}
    status = 'mixed' if low and counter else 'supported' if len(low_runs) >= MIN_REPEAT else 'weakened' if len(counter) >= MIN_REPEAT else 'unknown'
    for hid, label in HYPOTHESES:
        support_ids, counter_ids, unknown_ids, state = [], [], motive_ids, 'unknown'
        if hid == 'routine_group':
            support_ids, state = structure_ids, 'compatible'
        elif hid == 'assistance':
            support_ids, counter_ids, unknown_ids, state = low, counter, unknown + motive_ids, status
        elif hid == 'external_assistance':
            # Reverse evidence supports a competing explanation, not ownership
            # or commerce. Missing comparisons remain explicit unknowns.
            support_ids, counter_ids, unknown_ids = high, external_counter, unknown + motive_ids
            state = 'possible' if high else 'weakened' if external_counter else 'unknown'
        elif hid == 'later_progress':
            support_ids, state = own_ids, 'compatible'
        result['hypotheses'].append(dict(id=hid, label=label, status=state,
            support_ids=support_ids, counter_ids=counter_ids, unknown_ids=unknown_ids))
    for key, ids in sorted(missing.items(), key=lambda item: (item[0] not in perimeter, -len(item[1]), item[0])):
        result['questions'].append(dict(id=f'scored_runs:{key}', kind='scored_runs', identity_digest=key,
            run_ids=sorted(ids), hypothesis_ids=['assistance', 'external_assistance', 'routine_group', 'key_access'],
            priority=100 if key in perimeter else 50,
            reason='补外围当前同本成绩以区分低、高、近似方向；不能确认钥匙归属。' if key in perimeter
                else '补核心当前同本成绩，核验本人线索能否推广到稳定成员。'))
    result['questions'] = result['questions'][:MAX_QUESTIONS]
    if result['questions']:
        result['stop_reason'] = 'targeted_scores_needed'
    return result
