"""在同口径的完整场次中比较两名真实角色，保留来源和选样理由。"""

from collections import defaultdict
from fractions import Fraction
from urllib.parse import urlencode

from botend.services import combat_log_analysis as analysis

METRICS = {"damage": "每秒伤害（DPS）", "heal": "每秒有效治疗（HPS）"}


COUNT_METRICS = {"casts", "interrupts", "dispels", "deaths"}
DETAILS = {
    k: analysis.METRICS[k]
    for k in ("damage", "heal", "overheal", "taken", "casts", "interrupts", "dispels", "deaths")
}


def number_label(value):
    if value is None:
        return "—"
    if 0 < abs(value) < 0.01:
        return "<0.01"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def contrast(left, right):
    """相对差以较低值为分母。未知值与确认的零值分别处理。"""
    result = dict(left=number_label(left), right=number_label(right), side="", gap=None,
                  percent="", widths=(0, 0))
    if left is None or right is None:
        result["status"] = "数据不足"
        return result
    low, high = sorted((left, right))
    gap = high - low
    result.update(gap=gap, amount=number_label(gap), status="持平",
                  widths=tuple(round(v * 100 / high, 2) if high else 0 for v in (left, right)))
    if gap:
        result.update(side="A" if left > right else "B",
                      percent=(number_label(gap / low * 100) + "%") if low else "",
                      status="较高")
    return result


def directory(reports, target=""):
    """逐报告保留目录，允许手动选择内容相同的不同上传记录。"""
    groups, sources = {}, []
    for report in reports:
        samples, labels, _ = analysis.catalog([report])
        if not samples:
            continue
        parents = {s["id"]: s for s in samples}
        choices = []
        for s in samples:
            if target and s["cohort"] != target:
                continue
            if (
                s["start_ms"] is None
                or s["end_ms"] is None
                or s["end_ms"] <= s["start_ms"]
            ):
                continue
            group, title = s["cohort"], labels[s["cohort"]]
            if parent := parents.get(s["parent"]):
                group += "/" + parent["cohort"]
                title += " · " + parent["name"] + " +" + parent["level"]
                if parent["affixes"]:
                    title += " · 词缀 " + parent["affixes"]
            group += "/" + s["result"]
            groups[group] = title + " · " + s["result"]
            choices.append(dict(s, match_group=group))
        if choices:
            actors = report.summary.get("player_catalog")
            if target:
                axis = "keystone" if choices[0]["kind"] == "钥匙" else "boss"
                ids = [s["id"] for s in choices]
                markers = ",".join("?" for _ in ids)
                with analysis.database(report) as db:
                    actors = dict(db.execute(
                        f"SELECT guid,name FROM actor WHERE family='Player' AND id IN ("
                        f"SELECT src FROM fact WHERE {axis} IN ({markers}) UNION "
                        f"SELECT dst FROM fact WHERE {axis} IN ({markers}))", [*ids, *ids]))
            elif actors is None:
                with analysis.database(report) as db:
                    actors = dict(
                        db.execute(
                            "SELECT guid,name FROM actor WHERE family='Player' ORDER BY name,guid"
                        )
                    )
            sources.append(dict(report=report, choices=choices, actors=actors))
    return sources, groups


def candidates(sources, sides, metric, cohort):
    by_group = defaultdict(lambda: [[], []])
    seen = [set(), set()]
    for source in sources:
        report = source["report"]
        eligible = [
            (i, side)
            for i, side in enumerate(sides)
            if report.digest not in seen[i]
            and side["guid"] in source["actors"]
            and (not side["file"] or str(report.pk) == side["file"])
        ]
        if not eligible:
            continue
        for i, _ in eligible:
            seen[i].add(report.digest)
        with analysis.database(report) as db:
            for sample in source["choices"]:
                if cohort and cohort != sample["match_group"]:
                    continue
                wanted = [
                    (i, s)
                    for i, s in eligible
                    if not s["fight"] or s["fight"] == str(sample["id"])
                ]
                if not wanted:
                    continue
                guids = sorted({s["guid"] for _, s in wanted})
                placeholders = ",".join("?" for _ in guids)
                span = sample["end_ms"] - sample["start_ms"]
                if report.summary.get("player_totals_version") == 1:
                    rows = db.execute(
                        "SELECT guid,amount,hits,known FROM player_total WHERE fight=? AND metric=? "
                        + f" AND guid IN ({placeholders})",
                        [sample["id"], metric, *guids],
                    )
                else:
                    sql, args, value, _, _, _ = analysis.build_selection(
                        sample, {"pets": "owner"}, metric, None
                    )
                    rows = db.execute(
                        f"SELECT s.guid,SUM({value}),COUNT(*),COUNT({value})"
                        + sql
                        + f" AND s.guid IN ({placeholders}) GROUP BY s.guid",
                        [*args, *guids],
                    )
                scores = {r[0]: r for r in rows}
                for i, side in wanted:
                    score = scores.get(side["guid"])
                    if not score and side["fight"] and side["file"]:
                        # 指定场次时，确认参与过该场的玩家可以有零次输出或治疗。
                        axis = "keystone" if sample["kind"] == "钥匙" else "boss"
                        present = db.execute(
                            f"""SELECT 1 FROM actor a WHERE a.guid=? AND
                            (EXISTS (SELECT 1 FROM fact WHERE {axis}=? AND src=a.id)
                             OR EXISTS (SELECT 1 FROM fact WHERE {axis}=? AND dst=a.id))""",
                            (side["guid"], sample["id"], sample["id"]),
                        ).fetchone()
                        if present:
                            score = (side["guid"], 0, 0, 0)
                    if score and score[2] == score[3] and span:
                        by_group[sample["match_group"]][i].append(
                            dict(
                                sample=sample,
                                amount=score[1],
                                span=span,
                                hits=score[2],
                                score=Fraction(score[1] * 1000, span),
                            )
                        )
    return by_group


def comparison(sources, groups, sides, metric, cohort, detail):
    pool = candidates(sources, sides, metric, cohort)
    matches = []
    for group, pair in pool.items():
        if all(pair):
            winners = [max(items, key=lambda row: row["score"]) for items in pair]
            rates = sorted(w["score"] for w in winners)
            matches.append((rates, group, winners, pair))
    if matches:
        # 组间先最大化较低一侧的速率，再以另一侧的速率打破并列。
        _, group, winners, pair = max(matches, key=lambda item: (item[0], item[1]))
        title = groups[group]
    else:
        winners = []
    if len(winners) != 2:
        return {
            "compare_error": "没有同一副本或首领且条件一致的两场记录。请检查难度、战斗结果和所选角色。"
        }
    skills = {}
    bars = []
    detail_amounts, detail_rates = [], []
    longest = max(w["span"] for w in winners)
    step = max(10, ((longest + 599999) // 600000) * 10)
    for i, (side, winner) in enumerate(zip(sides, winners)):
        sample = winner["sample"]
        actor_axis = "target" if detail in ("taken", "deaths") else "source"
        params = dict(metric=detail, group="ability", pets="owner")
        params[actor_axis] = side["guid"]
        sql, args, value, _, _, _ = analysis.build_selection(
            sample, params, detail, None
        )
        base = sample["url"] + "&" + urlencode(params)
        factor = 60000 if detail in COUNT_METRICS else 1000
        with analysis.database(sample["report"]) as db:
            detail_total = 0
            for spell, name, total, hits, known in db.execute(
                f"SELECT f.spell,a.name,SUM({value}),COUNT(*),COUNT({value})"
                + sql
                + " GROUP BY f.spell,a.name",
                args,
            ):
                row = skills.setdefault(
                    spell, dict(name=name, spell=spell, sides=[None, None])
                )
                rate = total * factor / winner["span"] if known == hits else None
                detail_total = detail_total + total if detail_total is not None and rate is not None else None
                row["sides"][i] = dict(
                    amount=total,
                    hits=hits,
                    unknown=hits - known,
                    rate=rate,
                    url=base + "&" + urlencode(dict(ability=spell, panel="events")),
                )
            ticks = {
                r[0]: (r[1] if r[2] == r[3] else None)
                for r in db.execute(
                    f"SELECT (f.ms-?)/? AS tick,SUM({value}),COUNT(*),COUNT({value})"
                    + sql
                    + " GROUP BY tick",
                    [sample["start_ms"], step * 1000, *args],
                )
            }
        bars.append(ticks)
        detail_amounts.append(detail_total)
        detail_rates.append(detail_total * factor / winner["span"] if detail_total is not None else None)
        winner.update(
            event_url=base + "&panel=events",
            rate=float(winner["score"]),
            seconds=winner["span"] / 1000,
            name=side["name"],
            group_title=groups[sample["match_group"]],
            count=len(pair[i]),
            url=sample["url"]
            + "&"
            + urlencode(
                dict(metric=metric, source=side["guid"], pets="owner", group="ability")
            ),
        )
    for row in skills.values():
        rates = [s["rate"] if s else 0 for s in row["sides"]]
        row["diff"] = rates[1] - rates[0] if None not in rates else None
        row["contrast"] = contrast(*rates)
    timeline = []
    expected_ticks = range((longest - 1) // (step * 1000) + 1)
    ticks = sorted(set(expected_ticks).union(*bars))
    for tick in ticks:
        values = []
        for b, winner in zip(bars, winners):
            remaining = winner["span"] - tick * step * 1000
            if remaining <= 0 and tick not in b:
                values.append(None)
            else:
                values.append(b.get(tick, 0))
        timeline.append(dict(seconds=tick * step, values=values, contrast=contrast(*values)))
    peak = max(
        (v for row in timeline for v in row["values"] if v is not None), default=0
    )
    for row in timeline:
        row["bars"] = [
            dict(
                value=v,
                missing=v is None and row["seconds"] // step in bucket,
                width=v * 100 / peak if peak and v is not None else 0,
            )
            for v, bucket in zip(row["values"], bars)
        ]
    return dict(
        winners=winners,
        winning_group=title,
        common_count=len(matches),
        skills=sorted(
            skills.values(),
            key=lambda r: -max((s["rate"] or 0) for s in r["sides"] if s),
        ),
        difference=float(winners[1]["score"] - winners[0]["score"]),
        rate_contrast=contrast(*(w["rate"] for w in winners)),
        total_contrast=contrast(*(w["amount"] for w in winners)),
        detail_total_contrast=contrast(*detail_amounts),
        detail_rate_contrast=contrast(*detail_rates),
        timeline=timeline,
        step=step,
    )
