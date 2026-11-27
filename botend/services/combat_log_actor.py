"""角色施法时间线、光环已观测区间与地图分层的位置采样。"""

from collections import defaultdict

from botend.services import combat_log_visuals as visuals
from botend.services.combat_log_analysis import aura_coverage, database
from botend.services.combat_log_index import event_label


def inspect(report, fight_id, guid, after, start_offset=0, end_offset=None):
    with database(report) as db:
        actor = db.execute("SELECT * FROM actor WHERE guid=? AND family='Player'", (guid,)).fetchone()
        fight = db.execute("SELECT * FROM fight WHERE id=?", (fight_id,)).fetchone()
        if not actor or not fight or fight["start_line"] is None:
            raise LookupError
        bounds = [fight["start_line"], fight["end_line"]]
        if not db.execute(
            "SELECT 1 FROM fact WHERE line BETWEEN ? AND ? AND (src=? OR dst=?) LIMIT 1",
            [*bounds, actor["id"], actor["id"]],
        ).fetchone():
            raise LookupError
        actor = dict(actor)
        participant = visuals.participants(db, report, fight).get(guid, {})
        actor.update(visuals.identity(participant.get("specs", ()), participant.get("class_name")))
        source_icons = visuals.report_icons(db, report)
        builds = visuals.talents(db, report, fight, actor)
        origin = fight["start_ms"]
        casts = [
            dict(r, kind_label=event_label(r["kind"]), sec=round((r["ms"] - origin) / 1000, 3))
            for r in db.execute(
                """
            SELECT f.line,f.ms,f.kind,f.spell spell_id,a.name spell,t.name target FROM fact f JOIN ability a ON a.id=f.spell
            JOIN actor t ON t.id=f.dst WHERE f.line BETWEEN ? AND ? AND f.src=? AND f.line>?
            AND f.kind IN ('SPELL_CAST_START','SPELL_CAST_SUCCESS','SPELL_CAST_FAILED','SPELL_INTERRUPT','SPELL_DISPEL')
            ORDER BY f.line LIMIT 81""",
                [*bounds, actor["id"], after],
            )
        ]
        end = fight["end_ms"]
        if end is None:
            end = db.execute("SELECT MAX(ms) FROM fact WHERE line BETWEEN ? AND ?", bounds).fetchone()[0] or origin
        stop = min(end, origin + end_offset) if end_offset is not None else end
        start = min(origin + start_offset, stop)
        auras = []
        if report.summary.get("state_revision"):
            auras = aura_coverage(db, dict(fight, report=report), {"target": guid}, start, stop, include_debuffs=True)
        # 按时间桶选出一条真实采样，采样点和证据行保持一一对应。
        maps = defaultdict(list)
        step = max(1000, (end - origin) // 220)
        for r in db.execute(
            """SELECT s.* FROM sample s JOIN (SELECT min(line) chosen FROM sample
            WHERE actor=? AND line BETWEEN ? AND ? GROUP BY map,(ms-?)/?) g ON s.line=g.chosen ORDER BY s.line""",
            [actor["id"], *bounds, origin, step],
        ):
            maps[r["map"]].append(dict(r, sec=round((r["ms"] - origin) / 1000, 2)))
        for points in maps.values():
            x0, x1 = min(p["x"] for p in points), max(p["x"] for p in points)
            y0, y1 = min(p["y"] for p in points), max(p["y"] for p in points)
            scale = min(360 / max(1, x1 - x0), 144 / max(1, y1 - y0))
            for p in points:
                p.update(cx=round(10 + (p["x"] - x0) * scale, 2), cy=round(154 - (p["y"] - y0) * scale, 2))
    icons = visuals.spell_icons([c["spell_id"] for c in casts] + [a["spell"] for a in auras], source_icons)
    for c in casts:
        c["icon"] = icons.get(visuals.localization.number(c["spell_id"]))
    for a in auras:
        a["icon"] = icons.get(visuals.localization.number(a["spell"]))
    return dict(
        builds=builds, actor=actor,
        fight=fight,
        casts=casts[:80],
        cursor=casts[79]["line"] if len(casts) > 80 else None,
        auras=sorted(auras, key=lambda a: -a["covered_ms"]),
        aura_start=(start - origin) / 1000, aura_end=(stop - origin) / 1000,
        maps=dict(maps),
    )
