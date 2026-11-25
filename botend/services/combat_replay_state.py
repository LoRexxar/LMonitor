"""把实测光环转换成可定位的状态区间，并读取观察单位的生命和受击记录。"""

AURA_ACTIONS = {
    "SPELL_AURA_APPLIED": "获得", "SPELL_AURA_REFRESH": "刷新",
    "SPELL_AURA_APPLIED_DOSE": "叠层", "SPELL_AURA_REMOVED_DOSE": "减层",
    "SPELL_AURA_REMOVED": "移除", "SPELL_AURA_BROKEN": "打破",
    "SPELL_AURA_BROKEN_SPELL": "打破",
}
SCHEMA = """
CREATE TABLE replay_aura(line INTEGER PRIMARY KEY,session INTEGER,dst INTEGER,src INTEGER,
    spell TEXT,category TEXT,stacks INTEGER,action TEXT,ms INTEGER,end_ms INTEGER);
CREATE TABLE replay_hp(line INTEGER PRIMARY KEY,actor INTEGER,ms INTEGER,hp INTEGER,maximum INTEGER);
"""
INDEXES = """
CREATE INDEX replay_aura_unit ON replay_aura(session,dst,ms);
CREATE INDEX replay_sample_unit ON sample(actor,ms,line);
CREATE INDEX replay_fact_unit ON fact(dst,ms,line);
CREATE INDEX replay_death_unit ON fact(dst,ms,line) WHERE kind='UNIT_DIED';
CREATE INDEX replay_hp_unit ON replay_hp(actor,ms,line);
"""


def aura_fields(row):
    if row[0] not in AURA_ACTIONS:
        return None, None
    category = next((v for v in row[12:] if v in ("BUFF", "DEBUFF")), None)
    stacks = 1 if row[0] == "SPELL_AURA_APPLIED" else None
    if row[0].endswith("_DOSE") and len(row) > 13:
        value = row[13]
        if value.isascii() and value.isdigit() and len(value) < 9:
            stacks = int(value)
    return category, stacks


class AuraWriter:
    def __init__(self):
        self.active = {}

    def observe(self, writer, session, row, line, ms):
        event = row[0]
        if len(row) < 9:
            return
        if event == "UNIT_DIED":
            dst = writer.actors.get(row[5])
            for old_line, _ in self.active.pop(dst, {}).values():
                writer.db.execute("UPDATE replay_aura SET end_ms=? WHERE line=?", (ms, old_line))
            return
        if event not in AURA_ACTIONS or len(row) < 13 or not row[9].isdigit():
            return
        # 额外技能打破光环时，额外技能三元组位于光环类别之前。
        category, stacks = aura_fields(row)
        if not category:
            return
        src = writer.actor(row[1], row[2])
        dst = writer.actor(row[5], row[6])
        spell = writer.spell(row[9], row[10])
        key = (src, spell, category)
        active = self.active.setdefault(dst, {})
        ending = AURA_ACTIONS[event] in ("移除", "打破")
        if ending and key not in active and (event.startswith("SPELL_AURA_BROKEN") or row[1] in ("0", "0000000000000000")):
            matches = [k for k in active if k[1:] == key[1:]]
            if len(matches) == 1:
                key = matches[0]
        old = active.pop(key, None)
        if old is not None:
            writer.db.execute("UPDATE replay_aura SET end_ms=? WHERE line=?", (ms, old[0]))
        if ending:
            if not active:
                self.active.pop(dst, None)
            return
        if stacks == 0:
            return
        if event == "SPELL_AURA_REFRESH" and old is not None:
            stacks = old[1]
        writer.db.execute("INSERT INTO replay_aura VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                          (line, session, dst, src, spell, category, stacks, AURA_ACTIONS[event], ms))
        active[key] = (line, stacks)


def read_subject(db, report, fight, subject, start, end):
    from botend.services.combat_log_analysis import evidence_url
    from botend.services.combat_log_facts import DAMAGE, HEAL

    first, last, origin = fight["start_line"], fight["end_line"], fight["start_ms"]
    detailed = bool(report.summary.get("state_revision"))
    sources = [("fact", "src"), ("fact", "dst"), ("sample", "actor")]
    if detailed:
        sources.append(("replay_hp", "actor"))
    if not any(db.execute(
        f"SELECT 1 FROM {table} WHERE {col}=? AND line BETWEEN ? AND ? LIMIT 1",
        (subject, first, last),
    ).fetchone() for table, col in sources):
        raise LookupError
    auras = []
    if detailed:
        auras = [dict(r) for r in db.execute(
            """SELECT u.ms-? t,u.end_ms-? until,u.category,u.stacks,u.action,u.line,
                a.name spell,s.name source,u.src source_id,u.spell spell_id FROM replay_aura u
                JOIN actor s ON s.id=u.src JOIN ability a ON a.id=u.spell
                WHERE u.session=? AND u.dst=? AND u.ms<=? AND (u.end_ms IS NULL OR u.end_ms>=?)
                AND u.line<=? ORDER BY u.ms,u.line""",
            (origin, origin, fight["session"], subject, end, start, last)
        )]
    # 每个来源均取窗口前的最后一条；按时间和原文行号归并，拖动和顺播结果一致。
    health = []
    sources = [("sample", "actor", "hp,maximum", "")]
    if detailed:
        sources.append(("replay_hp", "actor", "hp,maximum", ""))
    sources.append(("fact", "dst", "0,NULL", " AND kind='UNIT_DIED'"))
    for table, actor_col, fields, condition in sources:
        sql = f"SELECT ms-?,line,{fields} FROM {table} WHERE {actor_col}=? AND line BETWEEN ? AND ?{condition}"
        params = (origin, subject, first, last)
        before = db.execute(sql+" AND ms<? ORDER BY ms DESC,line DESC LIMIT 1", (*params, start)).fetchone()
        if before:
            health.append(tuple(before))
        health.extend(map(tuple, db.execute(sql+" AND ms BETWEEN ? AND ?", (*params, start, end))))
    health.sort()
    categories = {**dict.fromkeys(DAMAGE, "承伤"), **dict.fromkeys(HEAL, "治疗"),
                  "SPELL_DISPEL": "驱散", "SPELL_RESURRECT": "复活施法", "UNIT_DIED": "死亡",
                  "SPELL_ABSORBED": "吸收", **AURA_ACTIONS}
    placeholders = ",".join("?" for _ in categories)
    events = []
    details = "f.aura_type,f.aura_stacks" if detailed else "NULL aura_type,NULL aura_stacks"
    for r in db.execute(
        f"""SELECT f.ms-? t,f.line,f.kind,f.amount,{details},a.name spell,s.name source,extra.name extra
        FROM fact f JOIN ability a ON a.id=f.spell JOIN actor s ON s.id=f.src
        LEFT JOIN ability extra ON extra.id=f.extra_spell
        WHERE f.dst=? AND f.line BETWEEN ? AND ? AND f.ms BETWEEN ? AND ? AND f.kind IN ({placeholders})
        ORDER BY f.ms,f.line""",
        (origin, subject, first, last, max(origin, start-5000), end, *categories)
    ):
        events.append(dict(r, label=categories[r["kind"]], url=evidence_url(report, r["line"])))
    return dict(auras=auras, health=health, events=events, detailed=detailed)


def party_aura_counts(db, fight, start, end):
    """将当前窗口内玩家的光环区间合并为计数变化，同毫秒刷新和换层不重复计数。"""
    bounds = (fight["start_line"], fight["end_line"])
    rows = db.execute(
        """SELECT u.dst,u.category,u.ms,u.end_ms FROM actor a CROSS JOIN replay_aura u
        WHERE a.family='Player' AND u.session=? AND u.dst=a.id AND u.ms<=?
        AND (u.end_ms IS NULL OR u.end_ms>?) AND u.line<=?
        AND (EXISTS (SELECT 1 FROM fact WHERE src=a.id AND line BETWEEN ? AND ?)
             OR EXISTS (SELECT 1 FROM fact WHERE dst=a.id AND line BETWEEN ? AND ?))""",
        (fight["session"], end, start, fight["end_line"], *bounds, *bounds),
    )
    changes = {}
    for actor, category, begin, finish in rows:
        column = 0 if category == "BUFF" else 1
        changes.setdefault((actor, max(start, begin)), [0, 0])[column] += 1
        if finish is not None and finish <= end:
            changes.setdefault((actor, finish), [0, 0])[column] -= 1
    result = []
    previous = None
    for (actor, ms), (buff, debuff) in sorted(changes.items()):
        if actor != previous:
            buff_count = debuff_count = 0
            previous = actor
        if buff or debuff:
            buff_count += buff
            debuff_count += debuff
            result.append((actor, ms-fight["start_ms"], buff_count, debuff_count))
    return result


def health_thresholds(db, report, fight, subject, percent, page, gap):
    """按真实时间及原文先后找进入阈值的采样；分别标明首样和采样空档。"""
    start, end = fight["start_line"], fight["end_line"]
    sources = ["sample"]
    if report.summary.get("state_revision"):
        sources.append("replay_hp")
    selects = [f"SELECT ms,line,hp,maximum FROM {table} WHERE actor=? AND line BETWEEN ? AND ?" for table in sources]
    rows = db.execute(
        f"""WITH samples AS ({" UNION ALL ".join(selects)}), history AS (
            SELECT *,hp*100.0/maximum pct,LAG(hp*100.0/maximum) OVER w prev,LAG(ms) OVER w prior
            FROM samples WINDOW w AS (ORDER BY ms,line))
            SELECT ms-? t,line,hp,maximum,CASE WHEN prev IS NULL THEN '首次采样'
            WHEN ms-prior>? THEN '采样间断后' ELSE '跌至阈值' END reason FROM history
            WHERE pct<=? AND (prev IS NULL OR prev>? OR ms-prior>?) ORDER BY ms,line LIMIT 21 OFFSET ?""",
        (*((subject, start, end)*len(sources)), fight["start_ms"], gap, percent, percent, gap, page*20)
    ).fetchall()
    return dict(samples=[dict(r) for r in rows[:20]], more=len(rows)>20)
