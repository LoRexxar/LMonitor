"""以开怪为零点对照同首领的各次尝试，仅使用有归属的实测事件。"""
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from itertools import groupby
from math import ceil

from django.core.paginator import Paginator
from django.urls import reverse

from botend.services import combat_log_analysis as analysis
from botend.services import combat_log_visuals as visuals
from botend.services import combat_log_localization as localization
from botend.services.combat_log_facts import DAMAGE
from botend.services.combat_replay_activity import boss_groups

ACTIONS = {
    "casts": ("成功施法", "SPELL_CAST_SUCCESS"),
    "failed": ("施法失败", "SPELL_CAST_FAILED"),
    "interrupts": ("成功打断", "SPELL_INTERRUPT"),
    "dispels": ("成功驱散", "SPELL_DISPEL"),
    "deaths": ("死亡", "UNIT_DIED"),
}
COLORS = ("#3483cf", "#d37818", "#138b78", "#a36bc4", "#c5536b", "#6b8b34")
ANCHOR_LIMIT = 200


def fields(query, *omit):
    return [(k, v) for k, values in query.lists() if k not in omit for v in values]


def link(params, **changes):
    q = params.copy()
    for k, v in changes.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = v
    return "?" + q.urlencode()


def event_links(sample, line, ms):
    offset = max(0, ms - sample["start_ms"])
    return dict(ms=offset, sec=round(offset / 1000, 1), line=line, evidence=analysis.evidence_url(sample["report"], line),
                replay=reverse("combat_replay", args=[sample["report"].pk, sample["id"]]) + f"?t={offset}")


def health(db, s):
    bosses = boss_groups(db, s["report"], s).get(-s["id"], [])
    if not bosses:
        return []
    tables = ["sample"]
    if s["report"].summary.get("state_revision", 0) >= 1:
        tables.append("replay_hp")
    marks = ",".join("?" for _ in bosses)
    condition = f"actor IN ({marks}) AND line BETWEEN ? AND ? AND ms BETWEEN ? AND ? AND maximum>0 AND hp BETWEEN 0 AND maximum"
    sources = [f"SELECT actor,ms,line,hp,maximum FROM {table} WHERE {condition}" for table in tables]
    # 每个时间桶取最后一条实测记录，同时保留该桶的最低值和首次经过各血量线的时间。
    bucket = max(1000, ceil(s["duration_ms"] / 450))
    query = f"""WITH samples AS ({' UNION ALL '.join(sources)}), ranked AS (
        SELECT actor,ms,line,100.0*hp/maximum pct,(ms-?)/? tick,
               ROW_NUMBER() OVER(PARTITION BY actor,(ms-?)/? ORDER BY ms DESC,line DESC) last
        FROM samples)
        SELECT r.actor,a.name,r.tick,MAX(CASE WHEN last=1 THEN ms END) ms,
               MAX(CASE WHEN last=1 THEN pct END) pct,MIN(r.pct) low,
               MIN(CASE WHEN pct<=75 THEN ms END),MIN(CASE WHEN pct<=50 THEN ms END),
               MIN(CASE WHEN pct<=25 THEN ms END),a.guid
        FROM ranked r JOIN actor a ON a.id=r.actor GROUP BY r.actor,r.tick ORDER BY r.actor,r.tick"""
    bounds = (*bosses, s["start_line"], s["end_line"], s["start_ms"], s["observed_end"])
    origin = s["start_ms"]
    rows = db.execute(query, (*bounds * len(sources), origin, bucket, origin, bucket))
    series = []
    for actor, grouped in groupby(rows, key=lambda r: r[0]):
        data = list(grouped)
        thresholds = []
        for i in (6, 7, 8):
            first = min((r[i] for r in data if r[i] is not None), default=None)
            thresholds.append(None if first is None else round((first-origin)/1000, 1))
        unit = str(localization.npc_id(data[0][9]) or data[0][1])
        series.append(dict(name=data[0][1], unit=unit, actor=actor, points=[[(r[3]-origin)/1000, round(r[4], 2)] for r in data],
                           minimum=round(min(r[5] for r in data), 1), thresholds=thresholds))
    return series


def health_at(curves, ms):
    second = ms / 1000
    observed = []
    for curve in curves:
        index = bisect_right(curve["points"], second, key=lambda point: point[0]) - 1
        if index >= 0:
            t, pct = curve["points"][index]
            if second - t <= 5:
                observed.append(dict(name=curve["name"], pct=pct, sec=t))
    return observed


def read_attempt(s, metric, at, player, sync):
    with analysis.database(s["report"]) as db:
        bounds = (s["id"], s["start_line"], s["end_line"])
        scope = "f.boss=? AND f.line BETWEEN ? AND ?"
        origin = s["start_ms"]
        end = s["end_ms"]
        if end is None:
            end = db.execute(f"SELECT MAX(f.ms) FROM fact f WHERE {scope}", bounds).fetchone()[0]
        s["observed_end"] = max(end or origin, origin)
        s["duration_ms"] = s["observed_end"] - origin
        s["duration"] = round(s["duration_ms"]/1000, 1)
        s["curves"] = localization.cached(s["report"], f"boss-review-hp:{s['id']}", lambda: health(db, s))
        s["anchors"], s["anchors_limited"] = localization.cached(
            s["report"], f"boss-review-anchors-v3:{s['id']}", lambda: read_anchors(db, s))
        center = None if at is None else at*1000
        if sync:
            if sync == "first-death":
                anchor = next((v for k,v in s["anchors"].items() if k[0] == "death"), None)
            else:
                anchor = next((v for k,v in s["anchors"].items() if json.dumps(k, ensure_ascii=False) == sync), None)
            center = anchor[2]["ms"] if anchor else None
            if anchor:
                s["sync_label"] = "各场首次玩家死亡" if sync == "first-death" else anchor[0]
                s["sync_sec"] = anchor[2]["sec"]
        s["roster"] = visuals.participants(db, s["report"], s)
        roster = {guid: actor["name"] for guid, actor in s["roster"].items()}
        start = origin if center is None else max(origin, origin+center-5000)
        stop = s["observed_end"] if center is None else min(s["observed_end"], origin+center+5000)
        if sync and center is None:
            stop = start-1
        s["covered"] = start <= stop
        s["coverage"] = round(max(0, stop-start)/1000, 1)
        where = scope + " AND f.ms BETWEEN ? AND ?"
        args = (*bounds, start, stop)
        side = "dst" if metric == "deaths" else "src"
        counts = dict(db.execute(f"""SELECT a.guid,COUNT(*) FROM fact f JOIN actor a ON a.id=f.{side}
            WHERE {where} AND f.kind=? AND a.family='Player' GROUP BY a.guid""", (*args, ACTIONS[metric][1])))
        s["people"] = {guid: (name, counts.get(guid, 0)) for guid, name in roster.items()}
        s["skills"] = {}
        if player in roster:
            for spell, label, count, first in db.execute(f"""SELECT f.spell,sp.name,COUNT(*),MIN(f.ms)
                FROM fact f JOIN actor a ON a.id=f.src JOIN ability sp ON sp.id=f.spell
                WHERE {where} AND a.guid=? AND f.kind='SPELL_CAST_SUCCESS' GROUP BY f.spell""", (*args, player)):
                s["skills"][spell] = (label, count, round((first-origin)/1000, 1))
        s["timeline"] = []
        if center is not None:
            labels = {code: name for name, code in ACTIONS.values()}
            kinds = ",".join("?" for _ in labels)
            events = db.execute(f"""SELECT f.kind,f.ms,f.line,a.name,sp.name,t.name FROM fact f
                JOIN actor a ON a.id=CASE WHEN f.kind='UNIT_DIED' THEN f.dst ELSE f.src END
                JOIN actor t ON t.id=f.dst LEFT JOIN ability sp ON sp.id=f.spell
                WHERE {where} AND f.kind IN ({kinds}) AND a.family='Player' ORDER BY f.ms,f.line LIMIT 41""",
                                (*args, *labels))
            for kind, ms, line, who, spell, target in events:
                s["timeline"].append(dict(who=who, action=labels[kind], spell=spell, target=target,
                                          relative=f"{(ms-origin-center)/1000:+.1f}",
                                          **event_links(s, line, ms)))
            s["more_events"] = len(s["timeline"]) > 40
            s["timeline"] = s["timeline"][:40]


def read_anchors(db, s):
    scope = "f.boss=? AND f.line BETWEEN ? AND ?"
    bounds = (s["id"], s["start_line"], s["end_line"])
    origin = s["start_ms"]
    anchors = {}
    limited = False
    occurrences = Counter()
    damage = sorted(DAMAGE)
    deaths = db.execute(f"""SELECT f.line,f.ms,a.guid,a.name,h.line,h.ms,sp.name,h.amount
        FROM fact f JOIN actor a ON a.id=f.dst LEFT JOIN fact h ON h.line=(
            SELECT hit.line FROM fact hit WHERE hit.dst=f.dst AND hit.boss=f.boss AND hit.line<f.line
            AND hit.ms BETWEEN f.ms-5000 AND f.ms AND hit.kind IN ({','.join('?' for _ in damage)})
            ORDER BY hit.ms DESC,hit.line DESC LIMIT 1)
        LEFT JOIN ability sp ON sp.id=h.spell
        WHERE {scope} AND f.kind='UNIT_DIED' AND a.family='Player' AND f.ms>=? ORDER BY f.line LIMIT ?""",
                        (*damage, *bounds, origin, ANCHOR_LIMIT+1))
    for i, (line, ms, guid, name, hit, hit_ms, spell, amount) in enumerate(deaths):
        if i == ANCHOR_LIMIT:
            limited = True
            break
        occurrences[guid] += 1
        detail = event_links(s, line, ms)
        detail["review"] = reverse("combat_review", args=[s["report"].pk, s["id"], "death", line])
        if hit is not None:
            detail.update(hit=analysis.evidence_url(s["report"], hit), hit_spell=spell,
                          hit_amount=amount, hit_before=round((ms-hit_ms)/1000, 1))
        anchors[("death", guid, occurrences[guid])] = (name+" 死亡", "死亡", detail)
    failures = db.execute(f"""SELECT f.line,f.ms,a.guid,a.name,f.spell,sp.name FROM fact f
        JOIN actor a ON a.id=f.src LEFT JOIN ability sp ON sp.id=f.spell
        WHERE {scope} AND f.kind='SPELL_CAST_FAILED' AND a.family='Player' AND f.ms>=?
        ORDER BY f.line LIMIT ?""", (*bounds, origin, ANCHOR_LIMIT+1))
    for i, (line, ms, guid, name, spell, label) in enumerate(failures):
        if i == ANCHOR_LIMIT:
            limited = True
            break
        failure = ("failed", guid, spell)
        occurrences[failure] += 1
        anchors[(*failure, occurrences[failure])] = (
            f"{name} · {label or spell}", "施法失败", event_links(s, line, ms))
    casts = db.execute("""SELECT o.line,f.ms,e.guid,e.name,o.spell,sp.name,o.outcome,o.response_line,r.ms,responder.name
        FROM opportunity o JOIN fact f ON f.line=o.line JOIN actor e ON e.id=o.enemy
        LEFT JOIN ability sp ON sp.id=o.spell LEFT JOIN fact r ON r.line=o.response_line
        LEFT JOIN actor responder ON responder.id=r.src
        WHERE o.boss=? AND o.line BETWEEN ? AND ? AND f.ms>=? ORDER BY o.line LIMIT ?""",
                       (*bounds, origin, ANCHOR_LIMIT+1))
    for i, (line, ms, guid, name, spell, label, result, response, response_ms, responder) in enumerate(casts):
        if i == ANCHOR_LIMIT:
            limited = True
            break
        unit_spell = (localization.npc_id(guid) or name, spell)
        occurrences[unit_spell] += 1
        detail = event_links(s, line, ms)
        if response is not None and response_ms is not None:
            detail["response"] = analysis.evidence_url(s["report"], response)
            detail["delay"] = round((response_ms-ms)/1000, 1)
            detail["responder"] = responder if result == "打断" else ""
        anchors[("cast", *unit_spell, occurrences[unit_spell])] = (
            f"{name} · {label or spell}", result, detail)
    return anchors, limited


def comparison(samples, params):
    brackets = sorted({s["key_bracket"] for s in samples})
    bracket = params.get("bracket", brackets[0] if brackets else "")
    if brackets and bracket not in brackets:
        raise ValueError("所选钥匙层数或词缀不在当前首领范围中。")
    kept, excluded = analysis.unique_attempts([s for s in samples if s["key_bracket"] == bracket])
    kept.sort(key=lambda s: (s["start_ms"], str(s["report"].pk), s["id"]))
    ctx = dict(excluded_attempts=excluded, bracket=bracket, brackets=brackets,
               bracket_fields=fields(params, "bracket", "base", "batch", "at"),
               metric_fields=fields(params, "behavior"), baseline_fields=fields(params, "base", "batch"),
               window_fields=fields(params, "at", "sync"), full_url=link(params, at=None, sync=None))
    if not kept:
        return ctx
    for idx, s in enumerate(kept, 1):
        s.update(key=f"{s['report'].pk}.{s['start_line']}", number=idx)
    base = next((s for s in kept if s["result"] == "击杀"), kept[0])
    if params.get("base"):
        base = next((s for s in kept if s["key"] == params["base"]), None)
        if base is None:
            raise ValueError("所选基准场次不在当前范围内。")
    page = Paginator([s for s in kept if s is not base], 5).get_page(params.get("batch"))
    shown = [base, *page]
    metric = params.get("behavior", "casts")
    if metric not in ACTIONS:
        raise ValueError("未知的玩家行为指标。")
    at = analysis.nonnegative(params, "at")
    if at is not None and at > 604800:
        raise ValueError("对齐时间超出范围。")
    player = params.get("player", "")
    sync = params.get("sync", "")
    if sync and (len(sync)>512 or at is not None):
        raise ValueError("请选择开怪时间或一个事件锚点进行对齐。")
    for s, color in zip(shown, COLORS):
        s["color"] = color
        read_attempt(s, metric, at, player, sync)
    duration = max(1, at or 0, ceil(max(s["duration"] for s in shown)))
    charts = []
    baseline_units = defaultdict(list)
    for curve in base["curves"]:
        baseline_units[curve["unit"]].append(curve)
    for s in shown:
        deaths = [v for k, v in s["anchors"].items() if k[0] == "death"]
        if deaths:
            name, _, first = min(deaths, key=lambda v: v[2]["sec"])
            s["first_death_anchor"] = dict(name=name, sec=first["sec"], url=link(params, at=round(first["sec"]), sync=None)+"#behavior")
        units = Counter(curve["unit"] for curve in s["curves"])
        for curve in s["curves"]:
            reference = baseline_units[curve["unit"]]
            comparable = s is not base and units[curve["unit"]] == 1 and len(reference) == 1
            curve["phases"] = []
            for index, sec in enumerate(curve["thresholds"]):
                other = reference[0]["thresholds"][index] if comparable else None
                delta = sec - other if sec is not None and other is not None else None
                curve["phases"].append(dict(sec=sec, delta=f"{delta:+.1f}" if delta else "",
                                            url=link(params, at=round(sec), sync=None)+"#behavior") if sec is not None else None)
            charts.append(dict(name=f"第 {s['number']} 次 · {curve['name']}", color=s["color"], unit=curve["unit"],
                               number=s["number"], baseline=s is base, points=curve["points"], end=s["duration"]))
    names = {guid: name for s in shown for guid, (name, _) in s["people"].items()}
    players = []
    for guid in sorted(names, key=lambda g: (names[g], g)):
        cells = []
        gaps = []
        reference = base["people"].get(guid) if base["covered"] else None
        for s in shown:
            person = s["people"].get(guid) if s["covered"] else None
            delta = person[1]-reference[1] if person and reference and s is not base else None
            if delta is not None:
                gaps.append(abs(delta))
            cells.append(dict(value=person[1] if person else None, delta=f"{delta:+}" if delta else "",
                              url=reverse("combat_actor", args=[s["report"].pk, s["id"], guid]) if person else ""))
        specs = {spec for sample in shown for spec in sample["roster"].get(guid, {}).get("specs", ())}
        players.append(dict(name=names[guid], guid=guid, **visuals.identity(specs), cells=cells, gap=max(gaps, default=-1),
                            url=link(params, player=guid)+"#player-skills"))
    players.sort(key=lambda row: -row["gap"])
    skills = []
    for spell in {k for s in shown for k in s["skills"]}:
        observed = [s["skills"].get(spell) for s in shown]
        counts = [(v[1] if v else 0) if s["covered"] and player in s["people"] else None
                  for v, s in zip(observed, shown)]
        cells = []
        for idx, (count, v) in enumerate(zip(counts, observed)):
            delta = count-counts[0] if idx and count is not None and counts[0] is not None else 0
            cells.append(dict(count=count, delta=f"{delta:+}" if delta else "", first=v[2] if v else None))
        gap = max((abs(n-counts[0]) for n in counts if n is not None and counts[0] is not None), default=-1)
        skills.append(dict(name=next(v[0] for v in observed if v), spell=spell, cells=cells, gap=gap,
                           total=sum(v for v in counts if v)))
    icons = visuals.spell_icons([s["spell"] for s in skills])
    for skill in skills:
        skill["icon"] = icons.get(localization.number(skill["spell"]))
    skills.sort(key=lambda r:(-r["gap"], -r["total"], r["spell"]))
    anchors = []
    for key in dict.fromkeys(k for s in shown for k in s["anchors"]):
        observed = [s["anchors"].get(key) for s in shown]
        label = next(v[0] for v in observed if v)
        results = {v[1] for v in observed if v}
        different = "完成" in results and "打断" in results
        times = [v[2]["sec"] for v in observed if v]
        cells = []
        for v, s in zip(observed, shown):
            if v:
                _, outcome, detail = v
                reference = observed[0]
                delta = detail["sec"]-reference[2]["sec"] if reference else None
                cells.append(dict(detail, outcome=outcome, hp=health_at(s["curves"], detail["ms"]),
                                  timing=f"{delta:+.1f}" if delta else "",
                                  window=link(params, at=round(detail["sec"]), sync=None) + "#behavior"))
            else:
                cells.append(None)
        anchors.append(dict(label=label, round=key[-1], flagged=key[0] in ("death", "failed") or different, different=different,
                            align=link(params, at=None, sync=json.dumps(key, ensure_ascii=False))+"#behavior",
                            time=min(times), cells=cells))
    anchors.sort(key=lambda a:(not a["flagged"], a["time"], a["label"], a["round"]))
    focused = [a for a in anchors if a["flagged"]]
    all_events = params.get("anchor_view") == "all"
    anchor_page = Paginator(anchors if all_events else focused, 30).get_page(params.get("anchor_page"))
    ctx.update(at=at, choices=kept, base=base, build=base["report"].summary["sessions"][base["session"]-1]["build"], shown=shown, charts=charts, duration=duration, players=players,
               sync=sync, sync_label=next((s["sync_label"] for s in shown if s.get("sync_label")), "未观测到所选锚点"),
               windowed=bool(sync) or at is not None,
               first_death_url=link(params, at=None, sync="first-death")+"#behavior" if any(s.get("first_death_anchor") for s in shown) else "",
               skills=skills, player_name=names.get(player, player), chosen_fields=fields(params, "attempt", "base", "batch", "bracket"),
               behavior=metric, metrics=[(k,v[0]) for k,v in ACTIONS.items()], anchors=anchor_page,
               anchor_count=len(anchors), focused_count=len(focused), all_events=all_events,
               anchor_toggle=link(params, anchor_view=None if all_events else "all", anchor_page=None)+"#anchors",
               anchor_prev=link(params, anchor_page=anchor_page.previous_page_number())+"#anchors" if anchor_page.has_previous() else "",
               anchor_next=link(params, anchor_page=anchor_page.next_page_number())+"#anchors" if anchor_page.has_next() else "",
               previous=link(params, batch=page.previous_page_number()) if page.has_previous() else "",
               following=link(params, batch=page.next_page_number()) if page.has_next() else "",
               page=page, window_start=max(0,(at or 0)-5), window_end=(at or 0)+5)
    return ctx
