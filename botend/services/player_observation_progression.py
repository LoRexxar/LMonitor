"""Second-stage, recent-only checks from validated central character/run facts.

Current scoring snapshots are a proxy, never a historical score delta. A missing
fifth does not erase the four core members' evidence or become a zero score.
"""
import math
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.services.raiderio_run_roster import run_reference

RECENT_WINDOW = timedelta(days=14)
SNAPSHOT_TTL = timedelta(hours=48)
CORE_LEVEL_GAP = 1
FIFTH_LEVEL_GAP = 2
MIN_CORE_COMPARISONS = 5
MIN_REPEAT = 3


def _date(value):
    try:
        stamp = parse_datetime(value) if isinstance(value, str) else value
        return stamp if stamp and timezone.is_aware(stamp) else None
    except (ValueError, TypeError, AttributeError):
        return None


def current_record(dungeons, dungeon):
    """Use the same matching and timed-alternate rule in both analysis stages."""
    matched = [row for row in dungeons if row['dungeon']['id'] == dungeon.get('id')
               and row['dungeon']['slug'] == dungeon.get('slug')]
    if len(matched) != 1:
        return None, set()
    all_records = [row for row in (matched[0], matched[0].get('alternate')) if row is not None]
    timed = [row for row in all_records if row['num_chests'] > 0]
    best = max(timed, key=lambda row: (row['mythic_level'], row['score'])) if timed else None
    return best, {row['keystone_run_id'] for row in all_records}


def _covered(known, total):
    return total > 0 and known * 5 >= total * 4


def build_progression(*, core, recent, runs, rosters, scores, score_times, fetched_at, rio_season, now):
    if core is None or len(core) != 4:
        return None
    stamp = _date(fetched_at)
    fresh = bool(stamp and now - SNAPSHOT_TTL <= stamp <= now)
    selected = {}
    for run in recent[:10]:
        try:
            rid = run_reference(run, rio_season)
        except (ValueError, TypeError):
            continue
        completed = _date(run.get('completed_at'))
        if (rid not in runs or completed is None or stamp is None
                or not stamp - RECENT_WINDOW <= completed <= stamp
                or type(run.get('num_keystone_upgrades')) is not int
                or run['num_keystone_upgrades'] <= 0):
            continue
        selected[rid] = (run, completed)
    core_runs, comparable, below, fifth_comparable, low = [], [], [], [], []
    low_fifths, details, visible = set(), [], 0
    for rid, (run, completed) in sorted(selected.items(), key=lambda item: (item[1][1], item[0]), reverse=True):
        if rid not in rosters:
            continue
        clean, keyed = rosters[rid]
        team = frozenset(key for key, _ in keyed)
        visible += 1
        if not core <= team:
            continue
        core_runs.append(rid)
        fifth = next(iter(team - core))
        records, contributions = {}, {}
        for key in team:
            score_stamp = score_times.get(key)
            if not score_stamp or not max(completed, now-SNAPSHOT_TTL) <= score_stamp <= now:
                continue
            record, ids = current_record(scores.get(key, []), clean['dungeon'])
            if record:
                records[key], contributions[key] = record, ids
        score = run.get('score')
        valid_score = type(score) in (int, float) and math.isfinite(score) and score >= 0
        core_known = core <= records.keys() and valid_score
        is_below, is_low = None, None
        if core_known:
            comparable.append(rid)
            is_below = all(records[key]['mythic_level'] - clean['mythic_level'] >= CORE_LEVEL_GAP
                and records[key]['score'] > score and rid not in contributions[key] for key in core)
            if is_below:
                below.append(rid)
                if fifth in records:
                    fifth_comparable.append(rid)
                    is_low = all(records[key]['mythic_level'] - records[fifth]['mythic_level'] >= FIFTH_LEVEL_GAP
                                 and records[key]['score'] > records[fifth]['score'] for key in core)
                    if is_low:
                        low.append(rid)
                        low_fifths.add(fifth)
        summary = lambda key: (f"{records[key]['mythic_level']}层/{records[key]['score']:g}分" if key in records else '未知')
        core_text = '、'.join(summary(key) for key in sorted(core))
        fifth_text = summary(fifth)
        verdict = ('四人核心均低于当前上限' if is_below else '未满足四人核心均低于当前上限' if is_below is False else '核心比较资料不足')
        if is_below:
            verdict += '；' + ('第五人明显偏低' if is_low else '第五人未明显偏低' if is_low is False else '第五人成绩未知')
        stamps = sorted(score_times[key] for key in records)
        collected = f'{stamps[0].isoformat()} 至 {stamps[-1].isoformat()}' if stamps else '未知'
        details.append({'id': f'progression:{rid}', 'run_id': rid, 'run_url': run['url'],
            'below_core': is_below, 'low_fifth': is_low,
            'text': f"{run['dungeon']} {clean['mythic_level']} 层；四人核心当前同本限时成绩：{core_text}；第五人：{fifth_text}。{verdict}。通关时间 {completed.isoformat()}；成绩采集时间 {collected}。这是当前成绩代理比较，不是历史加分。"})
    enough = (fresh and len(comparable) >= MIN_CORE_COMPARISONS
              and _covered(len(comparable), len(core_runs)) and _covered(visible, len(selected)))
    below_frequent = (len(below) >= MIN_REPEAT and len(below)*2 >= len(comparable)) if enough else None
    fifth_enough = fresh and len(fifth_comparable) >= MIN_REPEAT and _covered(len(fifth_comparable), len(below))
    low_frequent = (len(low) >= MIN_REPEAT and len(low)*2 >= len(fifth_comparable)
                    and len(low_fifths) >= 2) if fifth_enough else None
    if below_frequent is True and low_frequent is True:
        status, verdict = 'supported', '疑似四人带人队伍（低置信，近期双信号支持）；本人为稳定核心成员'
    elif below_frequent is False or (below_frequent is True and low_frequent is False):
        status, verdict = 'weakened', '四人核心轮换；近期递进核验未支持带人判断；本人为稳定核心成员'
    else:
        status, verdict = 'insufficient', '疑似四人带人队伍（低置信）；本人为稳定核心成员；近期递进证据不足，尚未确认双信号'
    freshness_note = '' if fresh else '近期记录快照缺失或已超过48小时，须补采后再判断。'
    scope = (f'仅取最近10条公开通关中、采集日前14天内的限时记录；命中 {len(selected)} 场，'
             f'阵容可验证 {visible} 场，该四人核心同场 {len(core_runs)} 场。'
             f'近期记录采集时间 {stamp.isoformat() if stamp else "未知"}。{freshness_note}')
    core_text = (f'{len(core_runs)} 场近期核心记录中，{len(comparable)} 场四人核心成绩可比，'
        f'{len(below)} 场四人均比当前同本限时计分层数低至少 {CORE_LEVEL_GAP} 层、单场分更低，且本场不在其当前计分记录（含alternate）中。'
        f'缺失 {len(core_runs)-len(comparable)} 场。此项是不加分层数的当前成绩代理，不是历史当场加分。')
    fifth_text = (f'上述 {len(below)} 场中，第五人成绩可比 {len(fifth_comparable)} 场；'
        f'{len(low)} 场第五人当前同本限时成绩比四人核心每人至少低 {FIFTH_LEVEL_GAP} 层且分数更低，'
        f'涉及 {len(low_fifths)} 名不同第五人；缺失 {len(below)-len(fifth_comparable)} 场。')
    describe = lambda value: '频繁出现' if value is True else '未频繁出现' if value is False else '证据不足'
    return {'status': status, 'verdict': verdict, 'scope': scope,
        'recent_timed_runs': len(selected), 'recent_roster_runs': visible, 'core_runs': len(core_runs),
        'core_comparable_runs': len(comparable), 'below_core_runs': len(below), 'below_core_run_ids': below,
        'fifth_comparable_runs': len(fifth_comparable), 'low_fifth_runs': len(low), 'low_fifth_run_ids': low,
        'distinct_low_fifths': len(low_fifths), 'missing_fifth_runs': len(below)-len(fifth_comparable),
        'below_core_frequent': below_frequent, 'low_fifth_frequent': low_frequent,
        'steps': [
            {'label': '第二步：核心反复完成不加分层数？', 'status': describe(below_frequent), 'text': core_text},
            {'label': '第三步：这些场次反复出现低分第五人？', 'status': describe(low_frequent), 'text': fifth_text}],
        'method': '频繁门槛：核心至少5场可比，命中至少3场且占可比场次一半；第五人至少3场可比，命中至少3场且占一半、涉及至少2名不同成员；相关资料覆盖均至少八成。后续进步、朋友互助和练号仍是替代解释。',
        'details': details}
