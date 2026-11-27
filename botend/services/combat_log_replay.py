"""按战斗行号隔离俯视回放，坐标和事件共用开战毫秒时钟。"""

from botend.services.combat_replay_state import health_thresholds, party_aura_counts, read_subject
from botend.services.combat_log_facts import DAMAGE, HEAL
from botend.services.combat_replay_maps import attach_floor_assets
from botend.constants.wow import SPEC_ROLE, specialization_catalog
from botend.services.combat_log_analysis import database, evidence_url
from botend.services.combat_log_index import dungeon_detail, level_label

from botend.services import combat_replay_activity as activity
from botend.services.combat_replay_activity import CHUNK_MS
GAP_MS = 4000
BUCKET_MS = 250


EVENT_PAGE_SIZE = 25
EVENT_KINDS = ("UNIT_DIED", "SPELL_INTERRUPT", "SPELL_CAST_START")


def _events(db, report, fight, start, end, *, query="", kind="", page=None):
    axis = "keystone" if fight["kind"] == "钥匙" else "boss"
    params = [fight["start_ms"], fight["id"], start, end, fight["start_line"], fight["end_line"]]
    details = "f.aura_type,f.aura_stacks" if report.summary.get("state_revision") else "NULL aura_type,NULL aura_stacks"
    sql = f"""SELECT {details},f.amount,f.ms-? t,f.line,f.kind,s.name source,d.name target,
        a.name spell,extra.name interrupted_spell,f.src source_id,f.dst target_id
        FROM fact f JOIN actor s ON s.id=f.src JOIN actor d ON d.id=f.dst
        JOIN ability a ON a.id=f.spell LEFT JOIN ability extra ON extra.id=f.extra_spell
        WHERE f.{axis}=? AND f.ms BETWEEN ? AND ? AND f.line BETWEEN ? AND ?"""
    if kind in ("BUFF", "DEBUFF"):
        if not report.summary.get("state_revision"):
            return []
        sql += " AND f.aura_type=?"
        params.append(kind)
    elif kind in ("DAMAGE", "HEAL"):
        kinds = DAMAGE if kind == "DAMAGE" else HEAL
        sql += " AND f.kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(kinds)
    elif kind == "SPELL_DISPEL":
        sql += " AND f.kind='SPELL_DISPEL'"
    else:
        sql += """ AND ((f.kind='UNIT_DIED' AND d.family='Player') OR f.kind='SPELL_INTERRUPT'
        OR (f.kind='SPELL_CAST_START' AND s.family IN ('Creature','Vehicle')))"""
        if kind:
            if kind not in EVENT_KINDS:
                raise ValueError("未知的回放事件类型。")
            sql += " AND f.kind=?"
            params.append(kind)
    if query:
        fields = ("s.name", "d.name", "a.name", "extra.name")
        sql += " AND (" + " OR ".join(f"instr(lower({field}),lower(?))>0" for field in fields)
        sql += """ OR EXISTS (SELECT 1 FROM main.ability raw WHERE raw.id IN (f.spell,f.extra_spell) AND instr(lower(raw.name),lower(?))>0)
                   OR EXISTS (SELECT 1 FROM main.actor raw WHERE raw.id IN (f.src,f.dst) AND instr(lower(raw.name),lower(?))>0)"""
        sql += " OR f.spell=? OR f.extra_spell=?)"
        params.extend([query] * (len(fields) + 4))
    sql += " ORDER BY f.ms,f.line"
    if page is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([EVENT_PAGE_SIZE + 1, page * EVENT_PAGE_SIZE])
    return [dict(r, url=evidence_url(report, r["line"])) for r in db.execute(sql, params)]


def read(report, fight_id, chunk=None, *, event_page=None, query="", kind="", subject=None, threshold=None, threshold_page=0, party_auras=False, casts_only=False):
    with database(report) as db:
        row = db.execute("SELECT * FROM fight WHERE id=?", (fight_id,)).fetchone()
        if not row:
            raise LookupError
        fight = dict(row)
        origin = fight["start_ms"]
        if origin is None or fight["start_line"] is None:
            if chunk is not None or event_page is not None or threshold is not None:
                raise LookupError
            return fight, None
        lines = (fight["start_line"], fight["end_line"])
        end = fight["end_ms"]
        if end is None:
            tables = ["fact", "sample"]
            if report.summary.get("state_revision"):
                tables.append("replay_hp")
            end = max(
                db.execute(
                    f"SELECT MAX(ms) FROM {table} WHERE line BETWEEN ? AND ?", lines
                ).fetchone()[0] or origin
                for table in tables
            )
        duration = max(0, end - origin)
        if threshold is not None:
            return fight, health_thresholds(db, report, fight, subject, threshold, threshold_page, GAP_MS)
        if event_page is not None:
            rows = _events(db, report, fight, origin, end, query=query, kind=kind, page=event_page)
            return fight, dict(events=rows[:EVENT_PAGE_SIZE], more=len(rows) > EVENT_PAGE_SIZE)
        if chunk is not None:
            if chunk > duration // CHUNK_MS:
                raise LookupError
            start = origin + chunk * CHUNK_MS
            if casts_only:
                return fight, {"casts": activity.read_casts(db, fight, start, min(end, start+CHUNK_MS-1),
                                                           activity.boss_groups(db, report, fight))}
            if subject is not None:
                return fight, read_subject(db, report, fight, subject, start, min(end, start+CHUNK_MS))
            lo, hi = max(origin, start - GAP_MS), min(end, start + CHUNK_MS + GAP_MS)
            event_start, event_end = max(origin, start-5000), min(end, start+CHUNK_MS-1)
            if kind:
                return fight, {"events": _events(db, report, fight, event_start, event_end, kind=kind)}
            current_index = report.summary.get("position_revision", 0) >= 2
            facing = "s.facing" if current_index else "NULL"
            by_time = "INDEXED BY sample_time" if current_index else ""
            # 保留每桶首尾实测点，并带上相邻窗口的采样以便连续播放。
            rows = db.execute(
                f"""WITH buckets AS (
                  SELECT min(line) first, max(line) last FROM sample {by_time}
                  WHERE line BETWEEN ? AND ? AND ms BETWEEN ? AND ?
                  GROUP BY actor,map,(ms-?)/?
                ), selected AS (SELECT first FROM buckets UNION SELECT last FROM buckets)
                SELECT s.actor,s.ms-?,s.map,s.x,s.y,s.hp,s.maximum,{facing}
                FROM selected JOIN sample s ON s.line=selected.first ORDER BY s.ms,s.line""",
                (*lines, lo, hi, origin, BUCKET_MS, origin),
            )
            points = [tuple(r) for r in rows]
            events = _events(db, report, fight, event_start, event_end)
            vitals = []
            if report.summary.get("state_revision"):
                vitals = [tuple(r) for r in db.execute(
                    """SELECT h.actor,h.ms-?,h.hp,h.maximum FROM replay_hp h JOIN actor a ON a.id=h.actor
                    WHERE a.family='Player' AND h.line BETWEEN ? AND ? AND h.ms BETWEEN ? AND ?
                    ORDER BY h.ms,h.line""", (origin, *lines, lo, hi)
                )]
            payload = dict(points=points, events=events, vitals=vitals)
            payload["activity"] = activity.read_chunk(db, fight, start, min(end, start+CHUNK_MS-1),
                                                      activity.boss_groups(db, report, fight))
            if party_auras and report.summary.get("state_revision"):
                payload["party_auras"] = party_aura_counts(db, fight, start, min(end, start+CHUNK_MS))
            return fight, payload

        maps = [dict(r) for r in db.execute(
            """SELECT map id,min(x) x0,max(x) x1,min(y) y0,max(y) y1,
            min(ms)-? first FROM sample WHERE line BETWEEN ? AND ? AND ms BETWEEN ? AND ?
            GROUP BY map ORDER BY first,map""", (origin, *lines, origin, end)
        )]
        instance_id = fight["game_id"] if fight["kind"] in ("钥匙", "地下城交战") else None
        fight["dungeon_detail"] = dungeon_detail(fight, wcl=hasattr(report, "wcl_source"))
        fight["level_label"] = level_label(fight["level"]) if fight["kind"] == "钥匙" else ""
        if fight["kind"] == "地下城交战":
            fight["level_label"] = "史诗" if fight["level"] == "23" else f"难度 {fight['level']}"
        if fight["parent"]:
            parent = db.execute("SELECT game_id,name,level FROM fight WHERE id=?", (fight["parent"],)).fetchone()
            instance_id = parent["game_id"] if parent else None
            if parent:
                fight.update(dungeon_name=parent["name"], level_label=level_label(parent["level"]))
        attach_floor_assets(maps, int(instance_id) if instance_id and str(instance_id).isdigit() else None)
        axis = "keystone" if fight["kind"] == "钥匙" else "boss"
        extra_hp = "UNION SELECT actor FROM replay_hp WHERE line BETWEEN ? AND ?" if report.summary.get("state_revision") else ""
        actors = [dict(r) for r in db.execute(
            f"""WITH participants AS (
                SELECT src id FROM fact WHERE {axis}=? UNION SELECT dst FROM fact WHERE {axis}=?
                UNION SELECT credited FROM fact WHERE {axis}=? AND credited IS NOT NULL
            ), subjects AS (
                SELECT actor FROM sample WHERE line BETWEEN ? AND ?
                UNION SELECT p.id FROM participants p JOIN actor a ON a.id=p.id WHERE a.family IN ('Player','Creature','Vehicle','Pet')
                {extra_hp}
            ) SELECT a.id,a.name,a.family,a.guid FROM actor a JOIN subjects s ON s.actor=a.id
            ORDER BY a.family='Player' DESC,a.name,a.id""",
            (fight_id, fight_id, fight_id, *lines, *(lines if extra_hp else ()))
        )]
        specs = {s["spec_id"]: s for s in specialization_catalog()}
        for actor in actors:
            if actor["family"] != "Player":
                continue
            spec_id = db.execute(
                """SELECT COALESCE(
                (SELECT src_spec FROM fact WHERE src=? AND line BETWEEN ? AND ? AND src_spec>0 ORDER BY line DESC LIMIT 1),
                (SELECT dst_spec FROM fact WHERE dst=? AND line BETWEEN ? AND ? AND dst_spec>0 ORDER BY line DESC LIMIT 1)
                )""", (actor["id"], *lines, actor["id"], *lines)
            ).fetchone()[0]
            if spec_id in specs:
                spec = specs[spec_id]
                actor.update(spec=spec["label"], class_label=spec["class_label"], color=spec["color"],
                             spec_icon=spec["icon"], role=SPEC_ROLE.get((spec["class_name"],spec["spec_name"])))
        deaths = [dict(r) for r in db.execute(
            """SELECT f.ms-? t,f.dst actor,a.name,f.line FROM fact f JOIN actor a ON a.id=f.dst
            WHERE f.line BETWEEN ? AND ? AND f.kind='UNIT_DIED' AND a.family='Player'
            AND f.ms BETWEEN ? AND ? ORDER BY f.ms,f.line""", (origin,*lines,origin,end)
        )]
        bosses = activity.boss_groups(db, report, fight)
        pulls = activity.pulls(db, fight, end)
        scopes = pulls if fight["kind"] == "钥匙" else [dict(id=-fight_id,t=0,end=duration)]
        boss_windows = [dict(t=p["t"], end=p["end"], actors=bosses[p["id"]]) for p in scopes if p["id"] in bosses]
        return fight, dict(duration=duration, maps=maps, actors=actors, deaths=deaths, boss_windows=boss_windows,
                           damage_buckets=activity.checkpoints(db, fight, end, report.summary.get("replay_damage_revision")),
                           pulls=pulls, cast_window=activity.CAST_WINDOW_MS,
                           cast_icons=activity.cast_icons(db, fight, bosses),
                           chunk=CHUNK_MS, gap=GAP_MS, revision=str(report.published))
