"""角色施法时间线、光环已观测区间与地图分层的位置采样。"""

from collections import defaultdict

from botend.services import combat_log_visuals as visuals
from botend.services.combat_log_analysis import database
from botend.services.combat_log_index import event_label


def inspect(report, fight_id, guid, after):
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
        # 同一施放者的同一光环只占一个区间，死亡封住当前已观测的区间。
        open_auras, auras = {}, {}
        last_ms = origin
        for r in db.execute(
            """SELECT f.line,f.ms,f.spell,f.src,f.kind,a.name FROM fact f JOIN ability a ON a.id=f.spell
            WHERE dst=? AND line BETWEEN ? AND ? ORDER BY line""",
            [actor["id"], *bounds],
        ):
            last_ms = r["ms"]
            key = r["src"], r["spell"]
            if r["kind"] == "UNIT_DIED":
                for k, start in open_auras.items():
                    auras[k]["ms"] += max(0, last_ms - start)
                open_auras.clear()
            if not r["kind"].startswith("SPELL_AURA_"):
                continue
            aura = auras.setdefault(
                key, dict(name=r["name"], spell=r["spell"], first=r["line"], ms=0, gaps=0)
            )
            if r["kind"] in ("SPELL_AURA_REMOVED", "SPELL_AURA_BROKEN", "SPELL_AURA_BROKEN_SPELL"):
                start = open_auras.pop(key, None)
                if start is not None:
                    aura["ms"] += max(0, last_ms - start)
                else:
                    aura["gaps"] += 1
            elif key not in open_auras:
                open_auras[key] = last_ms
                aura["gaps"] += int(r["kind"] != "SPELL_AURA_APPLIED")
        end = fight["end_ms"] or last_ms
        for key, start in open_auras.items():
            auras[key]["ms"] += max(0, end - start)
            auras[key]["gaps"] += int(fight["end_ms"] is None)
        for (src, _), aura in auras.items():
            aura["seconds"] = round(aura["ms"] / 1000, 3)
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
    icons = visuals.spell_icons([c["spell_id"] for c in casts] + [a["spell"] for a in auras.values()], source_icons)
    for c in casts:
        c["icon"] = icons.get(visuals.localization.number(c["spell_id"]))
    for a in auras.values():
        a["icon"] = icons.get(visuals.localization.number(a["spell"]))
    return dict(
        builds=builds, actor=actor,
        fight=fight,
        casts=casts[:80],
        cursor=casts[79]["line"] if len(casts) > 80 else None,
        auras=sorted(auras.values(), key=lambda a: -a["ms"]),
        maps=dict(maps),
    )
