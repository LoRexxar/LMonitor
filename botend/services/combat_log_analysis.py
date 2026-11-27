"""报告集合和单场使用共同的事件筛选、时间分母和证据定位。"""

from collections import Counter, defaultdict
from urllib.parse import urlencode

from django.urls import reverse

from botend.services.combat_log_archive import index_path
from botend.services.combat_log_facts import DAMAGE, HEAL, VERSION, pull_catalog
from botend.services import combat_log_localization as localization
from botend.services.combat_log_index import RAID_DIFFICULTIES, dungeon_detail, event_label, key_level, level_label
from botend.services import combat_log_visuals as visuals

FAILED_RESULTS = {"失败", "未击杀", "灭团", "未完成"}

def database(report):
    return localization.database(report,index_path(report))


def work_url(report, fight):
    return (
        reverse("combat_analysis", args=[report.pk]) + "?" + urlencode({"fight": fight})
    )


def evidence_url(report, line):
    return reverse("combat_evidence", args=[report.pk, line])


def catalog(reports):
    samples, skipped, groups = [], [], {}
    digests = set()
    for report in reports:
        if not report.published or report.summary.get("index_version", 0) < VERSION:
            skipped.append(f"{report.filename}（等待生成新版索引）")
            continue
        if report.digest in digests:
            skipped.append(f"{report.filename}（原文摘要重复）")
            continue
        digests.add(report.digest)
        activity_index = report.summary.get("activity_index")
        if activity_index is None:
            with database(report) as db:
                activity_index = [
                    dict(r) for r in db.execute("SELECT * FROM fight ORDER BY id")
                ]
        else:
            labels = localization.fight_labels(report,activity_index)
            activity_index = [dict(r,name=labels.get(r["id"],r["name"])) for r in activity_index]
        parents = {row["id"]: row for row in activity_index if row["kind"] == "钥匙"}
        for row in activity_index:
            session = report.summary["sessions"][row["session"] - 1]
            parent = parents.get(row["parent"])
            wcl = session.get("format") == "WCL"
            detail = dungeon_detail(row, wcl=wcl)
            raid = row["kind"] == "首领" and not detail and (wcl or str(row["level"]) in RAID_DIFFICULTIES)
            scope = (
                "钥匙"
                if row["kind"] == "钥匙"
                else ("地下城交战" if row["kind"] == "地下城交战" else "地下城首领" if detail else "首领")
            )
            cohort = "/".join(
                [
                    scope,
                    row["game_id"],
                    row["level"],
                    session["build"],
                    row["affixes"] or "",
                ]
            )
            key = row if row["kind"] == "钥匙" else parent
            key_label = level_label(key["level"]) if key else ""
            label = key_label if key else ("层数未知" if detail == "大秘境首领" else f"难度 {row['level']}")
            if row["kind"] == "地下城交战" and row["level"] == "23":
                label = "史诗（非钥匙）"
            title = f"{detail or scope} · {row['name']} · {label} · {session['build']}"
            if parent:
                cohort += f"/{parent['game_id']}/{parent.get('challenge_id') or ''}"
                title = f"{parent['name']} · {detail} · {row['name']} · {session['build']}"
            if row["affixes"]:
                title += " · 词缀 " + row["affixes"]
            groups[cohort] = title
            bracket = (f"{key_label} · 词缀 {parent['affixes'] or '未记录'}"
                       if parent else "")
            samples.append(dict(row, report=report, cohort=cohort, key_bracket=bracket, dungeon_detail=detail, raid=raid,
                                dungeon_name=key["name"] if key else "", level_label=label,
                                source_log=getattr(report, "source_filename", "") or report.filename,
                                comparable_key_boss=bool(parent and key_level(parent["level"]) and row["start_ms"] is not None
                                                         and row["game_id"] not in ("", "0")),
                                url=work_url(report, row["id"])))
    return samples, groups, skipped


def unique_attempts(samples, *, include_end_only=False):
    ranges = defaultdict(list)
    kept, excluded = [], []
    for sample in samples:
        begin, end = sample["start_ms"], sample["end_ms"]
        if begin is None:
            if include_end_only and end is not None:
                kept.append(sample)
                continue
            reason = "起点缺失"
        elif end is not None and any(
            pk != sample["report"].pk
            and roster
            and roster == sample.get("roster")
            and min(end, b) - max(begin, a) > 0.8 * min(end - begin, b - a)
            for pk, a, b, roster in ranges[sample["cohort"], sample.get("key_bracket", "")]
        ):
            reason = "观测阵容相同且时间高度重叠，仅采用先选视角"
        else:
            if end is not None:
                ranges[sample["cohort"], sample.get("key_bracket", "")].append(
                    (sample["report"].pk, begin, end, sample.get("roster"))
                )
            kept.append(sample)
            continue
        excluded.append(f"{sample['report'].filename} #{sample['id']}（{reason}）")
    return kept, excluded


def overview(samples):
    rows = {}
    kept, excluded = unique_attempts(samples, include_end_only=True)
    for s in kept:
        item = rows.setdefault(
            s["cohort"],
            dict(
                name=s["name"],
                kind=s["kind"],
                raid=s["raid"],
                level=s["level"],
                count=0,
                files=set(),
                cohort=s["cohort"],
                success=0,
                failed=0,
                deaths=0,
                kicks=0,
                partial=0,
                best=None,
                unit="",
                url="?" + urlencode({"cohort": s["cohort"]}),
            ),
        )
        item["count"] += 1
        item["files"].add(s["report"].pk)
        item["partial"] += int(s["end_ms"] is None)
        item["success"] += int(s["result"] in ("击杀", "已完成"))
        item["failed"] += int(s["result"] in FAILED_RESULTS)
        if s["start_ms"] is None:
            item["ending_records"] = item.get("ending_records", 0) + 1
            continue
        item["deaths"] += s.get("deaths", 0)
        item["kicks"] += s.get("kicks", 0)
        if s["kind"] == "钥匙":
            item["unit"] = "最快完成"
            if s["official_ms"] and s["result"] == "已完成":
                value = s["official_ms"] / 1000
                item["best"] = (
                    value if item["best"] is None else min(item["best"], value)
                )
        else:
            item["unit"] = "最晚首死"
            if s.get("first_death") is not None:
                value = (s["first_death"] - s["start_ms"]) / 1000
                item["best"] = (
                    value if item["best"] is None else max(item["best"], value)
                )
    return list(rows.values()), excluded


METRICS = {
    "damage": "造成伤害",
    "taken": "受到伤害",
    "heal": "有效治疗",
    "overheal": "过量治疗",
    "casts": "成功施法",
    "interrupts": "成功打断",
    "deaths": "死亡",
    "dispels": "成功驱散",
    "auras": "Buff",
    "enemycasts": "NPC 读条",
    "events": "全部索引事件",
}
GROUPS = {
    "source": "来源单位",
    "ability": "技能",
    "target": "目标单位",
    "spec": "专精",
    "role": "职责",
    "kind": "事件类型",
}
DIMENSIONS = {
    "source": ("s.guid", "s.name"),
    "target": ("t.guid", "t.name"),
    "ability": ("f.spell", "a.name"),
    "spec": ("sp.id", "sp.label"),
    "role": ("sp.role", "sp.role"),
    "kind": ("f.kind", "f.kind"),
}
JOINS = " FROM fact f JOIN actor s ON s.id=f.src JOIN actor t ON t.id=f.dst JOIN ability a ON a.id=f.spell "


def nonnegative(params, name, default=None):
    value = params.get(name)
    if value is None or value == "":
        return default
    if len(str(value)) > 12 or not str(value).isascii() or not str(value).isdigit():
        raise ValueError("区间与页码必须是非负整数。")
    return int(value)


def pull_filter(wave):
    membership = "f.boss=?" if wave["id"] < 0 else "f.wave=? AND COALESCE(f.boss,0)=0"
    return (membership + " AND f.line BETWEEN ? AND ? AND f.ms BETWEEN ? AND ?",
            [abs(wave["id"]),wave["first_line"],wave["last_line"],wave["start_ms"],wave["end_ms"]])


def build_selection(sample, params, metric, wave):
    db_axis = "keystone" if sample["kind"] == "钥匙" else "boss"
    clauses, binds = [f"f.{db_axis}=?"], [sample["id"]]
    origin = sample["start_ms"]
    start, stop = nonnegative(params, "start", 0), nonnegative(params, "end")
    if stop is not None and stop <= start:
        raise ValueError("结束秒数必须大于开始秒数。")
    lo = max(sample.get("from_ms", origin), origin + start * 1000)
    observed_end = sample["end_ms"] if sample["end_ms"] is not None else sample.get("observed_end_ms")
    bounds = [t for t in (observed_end, sample.get("to_ms"), origin + stop * 1000 if stop is not None else None)
              if t is not None]
    hi = min(bounds, default=None)
    clauses.append("f.ms>=?")
    binds.append(lo)
    if stop is not None or "to_ms" in sample:
        clauses.append("f.ms<?")
        binds.append(min(t for t in (sample.get("to_ms"), origin + stop * 1000 if stop is not None else None)
                         if t is not None))
    if wave is not None:
        condition, bounds = pull_filter(wave)
        clauses.append(condition)
        binds.extend(bounds)
        lo = max(lo, wave["start_ms"])
        hi = min(hi, wave["end_ms"]) if hi is not None else wave["end_ms"]
    family = "t" if metric in ("taken", "deaths", "auras") else "s"
    pets = params.get("pets", "players")
    if pets not in ("players", "separate", "owner"):
        raise ValueError("未知的召唤物归属方式。")
    side = params.get("side", "players")
    if side not in ("players", "npc", "all"):
        raise ValueError("未知的单位范围。")
    joins = JOINS
    if family == "s" and pets == "owner" and metric not in ("enemycasts", "events"):
        joins = joins.replace("s.id=f.src", "s.id=f.credited")
    if metric == "enemycasts":
        clauses.append("s.family IN ('Creature','Vehicle')")
    elif metric != "events" and side == "npc":
        clauses.append(f"{family}.family IN ('Creature','Vehicle')")
    elif metric == "events" or side == "all":
        pass
    elif family == "s" and pets == "separate":
        clauses.append("(s.family IN ('Player','Pet') OR f.src<>f.credited)")
    else:
        clauses.append(f"{family}.family='Player'")
    joins += (
        " JOIN spec sp ON sp.id=f."
        + ("dst_spec" if family == "t" else "src_spec")
        + " "
    )
    kinds = {
        "damage": DAMAGE,
        "taken": DAMAGE,
        "heal": HEAL,
        "overheal": HEAL,
        "casts": ["SPELL_CAST_SUCCESS"],
        "interrupts": ["SPELL_INTERRUPT"],
        "deaths": ["UNIT_DIED"],
        "dispels": ["SPELL_DISPEL"],
        "auras": ["SPELL_AURA_APPLIED"],
        "enemycasts": ["SPELL_CAST_START"],
        "events": [],
    }[metric]
    if kinds:
        clauses.append("f.kind IN (" + ",".join("?" for _ in kinds) + ")")
        binds.extend(kinds)
    if metric == "auras":
        clauses.append("f.aura_type='BUFF'" if sample["report"].summary.get("state_revision") else "0")
    for dim, (column, _) in DIMENSIONS.items():
        if params.get(dim):
            clauses.append(column + "=?")
            binds.append(params[dim])
    value = {
        "damage": "f.amount",
        "taken": "f.amount",
        "heal": "f.amount",
        "overheal": "f.excess",
    }.get(metric, "1")
    known = wave["known"] if wave else observed_end is not None
    span = max(0, hi - lo) if hi is not None and known else None
    return joins + " WHERE " + " AND ".join(clauses), binds, value, span, lo, hi


def aura_coverage(db, sample, params, start, end, *, include_debuffs=False):
    """按目标和技能合并实测状态区间，保留选区左侧已生效的 Buff。"""
    if end <= start:
        return []
    axis = "keystone" if sample["kind"] == "钥匙" else "boss"
    conditions = ["u.session=?", "u.line<=?", "u.ms<?",
                  "(u.end_ms IS NULL OR u.end_ms>?)"]
    args = [sample["session"], sample["end_line"], end, start]
    if not include_debuffs:
        conditions.append("u.category='BUFF'")
    side = params.get("side", "players")
    if side == "players":
        conditions.append("t.family='Player'")
    elif side == "npc":
        conditions.append("t.family IN ('Creature','Vehicle')")
    for key, field in (("source", "s.guid"), ("target", "t.guid"), ("ability", "u.spell")):
        if params.get(key):
            conditions.append(field + "=?")
            args.append(params[key])
    if params.get("kind"):
        conditions.append("u.line IN (SELECT line FROM fact WHERE kind=?)")
        args.append(params["kind"])
    roster_conditions, roster_args = ["roster.actor=u.dst"], []
    for key, field in (("spec", "sp.id"), ("role", "sp.role")):
        if params.get(key):
            roster_conditions.append(field + "=?")
            roster_args.append(params[key])
    rows = db.execute(f"""
        WITH roster(actor,spec) AS (
            SELECT src,src_spec FROM fact WHERE {axis}=? UNION SELECT dst,dst_spec FROM fact WHERE {axis}=?
        )
        SELECT t.guid,t.name,u.spell,a.name,u.category,u.ms,u.end_ms,u.line,s.guid,s.name
        FROM replay_aura u JOIN actor t ON t.id=u.dst JOIN actor s ON s.id=u.src
        JOIN ability a ON a.id=u.spell
        WHERE EXISTS (SELECT 1 FROM roster JOIN spec sp ON sp.id=roster.spec
                      WHERE {" AND ".join(roster_conditions)})
        AND {" AND ".join(conditions)}
        ORDER BY t.guid,u.spell,u.category,u.ms,u.line
    """, [sample["id"], sample["id"], *roster_args, *args])
    result = []
    previous = None
    for guid, target, spell, name, category, begin, finish, line, source, source_name in rows:
        stop = min(finish if finish is not None else end, end)
        if stop <= max(begin, start):
            continue
        key = (guid, spell, category)
        if key != previous:
            previous, covered_until = key, start
            item = dict(target=target, guid=guid, spell=spell, name=name, covered_ms=0, category=category,
                        first=line, sources={},
                        window_ms=end-start, filename=sample["report"].filename, fight=sample["id"])
            result.append(item)
        item["sources"][source] = source_name
        item["covered_ms"] += max(0, stop - max(begin, covered_until))
        covered_until = max(covered_until, stop)
    for item in result:
        item.update(percent=round(100 * item["covered_ms"] / item["window_ms"], 2),
                    seconds=item["covered_ms"] / 1000, window_seconds=item["window_ms"] / 1000)
    return result


def run(samples, params, *, single=False, first_wave=False):
    metric = params.get("metric", "damage")
    group = params.get("group") or (
        "target" if metric in ("taken", "deaths", "auras") else "source"
    )
    if metric not in METRICS or group not in GROUPS:
        raise ValueError("未知的指标或分组。")
    wave_id = params.get("wave")
    if wave_id and wave_id != "all" and (
        not single or len(samples) != 1 or samples[0]["kind"] != "钥匙"
    ):
        raise ValueError("波次筛选需要选择一把钥匙。")
    dim, label = DIMENSIONS[group]
    panel = params.get("panel", "table")
    cross = params.get("cross") or ("ability" if group != "ability" else "target")
    if cross not in DIMENSIONS or (panel == "pivot" and cross == group):
        raise ValueError("请选择不同的行维度和列维度。")
    column, column_label = DIMENSIONS[cross]
    cells, headers = {}, {}
    event_skip = max(0, nonnegative(params, "eventpage", 1) - 1) * 50
    matched_hits = 0
    series = (
        params.getlist("series")
        if hasattr(params, "getlist")
        else params.get("series", [])
    )
    series = list(dict.fromkeys(series)) if params.get("seriesgroup") == group else []
    if len(series) > 4:
        raise ValueError("同图最多比较四条曲线。")
    traces = {key: Counter() for key in series}
    groups, buckets = {}, Counter()
    comparisons, skipped = [], []
    unknown = total = duration = 0
    duration_known = True
    events, waves, mechanisms, players = [], [], [], []
    coverage, coverage_unavailable = [], []
    actor_specs = defaultdict(set)
    actor_classes, report_icons = {}, {}
    wave = None
    after = nonnegative(params, "after", 0)
    anchor = nonnegative(params, "anchor")
    ordinal = nonnegative(params, "occurrence", 1)
    before, beyond = nonnegative(params, "before", 3), nonnegative(params, "beyond", 8)
    if anchor and (
        not 1 <= ordinal <= 1000 or before + beyond == 0 or max(before, beyond) > 3600
    ):
        raise ValueError("机制轮次应在 1–1000 之间，窗口须大于零且每侧不超过一小时。")
    included, skipped = unique_attempts(samples)
    for sample in included:
        report, origin = sample["report"], sample["start_ms"]
        with database(report) as db:
            axis = "keystone" if sample["kind"] == "钥匙" else "boss"
            reference = origin
            if single and sample["kind"] == "钥匙":
                waves = pull_catalog(db,sample,include_enemies=True)
                if first_wave and waves and not wave_id:
                    wave = waves[0]
                elif wave_id and wave_id != "all":
                    wave = next((w for w in waves if str(w["id"]) == str(wave_id)), None)
                    if wave is None:
                        raise ValueError("该钥匙中没有选定波次。")
            if anchor:
                condition, bounds = pull_filter(wave) if wave else ("1=1", [])
                hit = db.execute(
                    f"SELECT f.ms FROM opportunity o JOIN fact f ON o.line=f.line WHERE o.{axis}=? "
                    f"AND o.spell=? AND {condition} ORDER BY o.line LIMIT 1 OFFSET ?",
                    (sample["id"], str(anchor), *bounds, ordinal - 1),
                ).fetchone()
                if not hit:
                    skipped.append(
                        f"{report.filename} #{sample['id']}（未观测到选定机制轮次）"
                    )
                    continue
                reference = hit[0]
                sample = dict(
                    sample,
                    from_ms=max(origin, reference - before * 1000),
                    to_ms=reference + beyond * 1000,
                )
            sql, args, value, span, window_start, window_end = build_selection(
                sample, params, metric, wave
            )
            coverage_picker = panel == "pick" and group in ("ability", "target", "source")
            if metric == "auras" and (panel == "table" or coverage_picker):
                if not report.summary.get("state_revision") or window_end is None:
                    coverage_unavailable.append(f"{report.filename} #{sample['id']}")
                else:
                    active = aura_coverage(db, sample, params, window_start, window_end)
                    if coverage_picker:
                        for buff in active:
                            choices = (buff["sources"].items() if group == "source" else
                                       [(buff["guid"], buff["target"])] if group == "target" else
                                       [(buff["spell"], buff["name"])])
                            for ident, name in choices:
                                groups.setdefault(ident, dict(ident=ident, name=name, value=0, hits=0, unknown=0))
                    else:
                        coverage.extend(active)
            rows = db.execute(
                f"SELECT {dim} ident, min({label}) name, sum({value}) value, count(*) hits, "
                f"count({value}) known " + sql + f" GROUP BY {dim}",
                args,
            )
            subtotal = missing = hits = 0
            for r in rows:
                item = groups.setdefault(
                    r["ident"],
                    dict(ident=r["ident"], name=r["name"], value=0, hits=0, unknown=0),
                )
                item["value"] += r["value"] or 0
                item["hits"] += r["hits"]
                item["unknown"] += r["hits"] - r["known"]
                subtotal += r["value"] or 0
                hits += r["hits"]
                missing += r["hits"] - r["known"]
            period = 10000
            for r in db.execute(
                f"SELECT (f.ms-?-CASE WHEN f.ms<? THEN ? ELSE 0 END)/? bucket,sum({value}) value "
                + sql
                + " GROUP BY bucket",
                [reference, reference, period - 1, period, *args],
            ):
                buckets[r["bucket"] * 10] += r["value"] or 0
            if series:
                placeholders = ",".join("?" for _ in series)
                for tick, ident, amount in db.execute(
                    f"SELECT (f.ms-?-CASE WHEN f.ms<? THEN ? ELSE 0 END)/? bucket, {dim}, sum({value}) "
                    + sql
                    + f" AND CAST({dim} AS TEXT) IN ({placeholders}) GROUP BY bucket,{dim}",
                    [reference, reference, period - 1, period, *args, *series],
                ):
                    traces[str(ident)][tick * 10] += amount or 0
            if panel == "pivot":
                for cell in db.execute(
                    f"SELECT {dim}, {column}, min({column_label}), sum({value}), count(*), count({value}) "
                    + sql
                    + f" GROUP BY {dim},{column}",
                    args,
                ):
                    header = headers.setdefault(
                        cell[1], dict(ident=cell[1], name=cell[2], value=0, hits=0)
                    )
                    header["value"] += cell[3] or 0
                    header["hits"] += cell[4]
                    entry = cells.setdefault(
                        (cell[0], cell[1]), dict(value=0, hits=0, unknown=0)
                    )
                    entry["value"] += cell[3] or 0
                    entry["hits"] += cell[4]
                    entry["unknown"] += cell[4] - cell[5]
            total += subtotal
            unknown += missing
            duration += span or 0
            duration_known &= span is not None
            comparisons.append(
                dict(
                    sample,
                    value=subtotal,
                    hits=hits,
                    unknown=missing,
                    window_start=window_start,
                    window_end=window_end,
                    seconds=None if span is None else round(span / 1000, 2),
                    rate=round(subtotal * 1000 / span, 2)
                    if span and not missing
                    else None,
                )
            )
            report_icons.update(visuals.report_icons(db, report))
            if single or group in ("source", "target") or (panel == "pivot" and cross in ("source", "target")) or panel == "events":
                roster = visuals.participants(db, report, sample)
                for guid, actor in roster.items():
                    actor_specs[guid].update(actor["specs"])
                    if actor.get("class_name"):
                        actor_classes[guid] = actor["class_name"]
                if single:
                    players = [dict(actor, **visuals.identity(actor["specs"], actor.get("class_name"))) for actor in roster.values()]
            if (
                (panel == "events" or single)
                and len(events) < 51
                and event_skip < matched_hits + hits
            ):
                for r in db.execute(
                    f"SELECT f.line,f.ms,f.kind,s.name source,t.name target,a.name spell,f.spell spell_id,s.guid source_guid,t.guid target_guid,f.src_spec source_spec,f.dst_spec target_spec,{value} value "
                    + sql
                    + " AND f.line>? ORDER BY f.line LIMIT ? OFFSET ?",
                    [*args, after, 51 - len(events), max(0, event_skip - matched_hits)],
                ):
                    events.append(
                        dict(
                            r,
                            kind_label=event_label(r["kind"]),
                            seconds=round((r["ms"] - reference) / 1000, 3),
                            replay_url=reverse("combat_replay", args=[report.pk, sample["id"]])
                            + "?" + urlencode({"t": max(0, r["ms"] - origin)}),
                            url=evidence_url(report, r["line"]),
                            sample=sample["id"],
                            filename=report.filename,
                            review_url=reverse(
                                "combat_review",
                                args=[report.pk, -wave["id"] if wave and wave["id"] < 0 else sample["id"], "death", r["line"]],
                            )
                            if r["kind"] == "UNIT_DIED"
                            and metric == "deaths"
                            and params.get("side", "players") == "players"
                            else None,
                        )
                    )
            matched_hits += hits
            if "panel" in params and panel != "review":
                continue
            condition, condition_args = [f"o.{axis}=?"], [sample["id"]]
            if wave:
                pull_clause, bounds = pull_filter(wave)
                condition.append(pull_clause)
                condition_args.extend(bounds)
            condition.append("f.ms>=?")
            condition_args.append(
                sample.get("from_ms", origin + nonnegative(params, "start", 0) * 1000)
            )
            stop = nonnegative(params, "end")
            if stop is not None or anchor:
                condition.append("f.ms<?")
                condition_args.append(
                    sample["to_ms"] if anchor else origin + stop * 1000
                )
            for r in db.execute(
                "SELECT o.spell,a.name,o.outcome,count(*) n,min(o.line) example "
                "FROM opportunity o JOIN ability a ON a.id=o.spell JOIN fact f ON f.line=o.line WHERE "
                + " AND ".join(condition)
                + " GROUP BY o.spell,o.outcome",
                condition_args,
            ):
                mechanisms.append(
                    dict(r, report=report, url=evidence_url(report, r["example"]))
                )
    ranked = sorted(groups.values(), key=lambda r: (-r["value"], r["ident"]))
    for row in ranked:
        row["share"] = round(row["value"] * 100 / total, 1) if total else 0
    # 保留无事件的时间间隔；长战斗合并相邻桶以限制图形宽度。
    step = 10
    if buckets:
        step = max(1, (max(buckets) - min(buckets)) // 1800 + 1) * 10
        merged = Counter()
        for t, v in buckets.items():
            merged[t // step * step] += v
        buckets = {t: merged[t] for t in range(min(merged), max(merged) + 1, step)}
    peak = max(buckets.values(), default=0)
    trend = [
        dict(seconds=t, value=v, percent=100 * v / peak if peak else 0)
        for t, v in sorted(buckets.items())
    ]
    names = {str(row["ident"]): row["name"] for row in ranked}
    curves = []
    times = sorted(buckets)
    for key, raw in traces.items():
        bins = Counter()
        for t, amount in raw.items():
            bins[t // step * step] += amount
        curves.append(
            dict(ident=key, name=names.get(key, key), values=[bins[t] for t in times])
        )
    curve_peak = max((max(curve["values"], default=0) for curve in curves), default=0)
    for curve in curves:
        values = curve["values"]
        pairs = [
            (1000 * i / max(1, len(values) - 1), 100 - 100 * v / max(1, curve_peak))
            for i, v in enumerate(values)
        ]
        if len(pairs) == 1:
            pairs.append((1000, pairs[0][1]))
        curve["points"] = " ".join(f"{x:.2f},{y:.2f}" for x, y in pairs)
    rules = {}
    for m in mechanisms:
        rule = rules.setdefault(
            m["spell"], {"spell": m["spell"], "name": m["name"], "outcomes": {}, "n": 0}
        )
        item = rule["outcomes"].setdefault(m["outcome"], {"n": 0, "url": m["url"]})
        item["n"] += m["n"]
        rule["n"] += m["n"]
    return dict(
        metric=metric,
        curves=curves,
        curve_peak=curve_peak,
        group=group,
        cross=cross,
        cells=cells,
        headers=sorted(
            headers.values(), key=lambda x: (-x["value"], -x["hits"], str(x["ident"]))
        ),
        matched_hits=matched_hits,
        metrics=METRICS,
        dimensions=GROUPS,
        total=total,
        unknown=unknown,
        rate=round(total * 1000 / duration, 2)
        if duration_known and duration and not unknown
        else None,
        rows=ranked,
        coverage=coverage,
        coverage_unavailable=coverage_unavailable,
        trend=trend,
        step=step,
        comparisons=comparisons,
        waves=waves,
        active_wave=wave,
        players=players,
        actor_specs=actor_specs, actor_classes=actor_classes, report_icons=report_icons,
        events=events[:50],
        next_line=events[49]["line"] if len(events) > 50 else None,
        excluded=skipped,
        rules=sorted(rules.values(), key=lambda rule: -rule["n"]),
    )
