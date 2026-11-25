"""把原文字节流索引为可重建的单报告战斗目录与玩家事件。"""

import csv
import hashlib
import re
import sqlite3
from datetime import date
from functools import lru_cache

from botend.services import combat_log_facts as facts
from botend.services import combat_replay_activity, combat_replay_state

STAMP = re.compile(
    rb"(\d{1,2})/(\d{1,2})(?:/(\d{4}))? (\d{1,2}):(\d{2}):(\d{2})\.(\d{3})(?:([+-])(\d{1,2})(?::(\d{2}))?)?"
)
MAX_LINE = 2 * 1024 * 1024
PLAYER_EVENTS = {
    "UNIT_DIED": "死亡",
    "SPELL_DAMAGE": "技能伤害",
    "SPELL_PERIODIC_DAMAGE": "持续伤害",
    "RANGE_DAMAGE": "远程伤害",
    "SWING_DAMAGE": "近战伤害",
    "ENVIRONMENTAL_DAMAGE": "环境伤害",
    "SPELL_HEAL": "治疗",
    "SPELL_PERIODIC_HEAL": "持续治疗",
    "SPELL_ABSORBED": "吸收",
    "SPELL_AURA_APPLIED": "获得光环",
    "SPELL_AURA_REMOVED": "失去光环",
    "SPELL_RESURRECT": "战复施法",
    "DAMAGE_SHIELD": "反弹伤害",
    "DAMAGE_SPLIT": "分摊伤害",
    "SPELL_DISPEL": "驱散",
    "SPELL_INTERRUPT": "打断",
    "SPELL_CAST_SUCCESS": "施法成功",
}
BOUNDARIES = {
    "COMBAT_LOG_VERSION",
    "ENCOUNTER_START",
    "ENCOUNTER_END",
    "CHALLENGE_MODE_START",
    "CHALLENGE_MODE_END",
}

EVENT_LABELS = {
    **{code: label + "光环" for code, label in combat_replay_state.AURA_ACTIONS.items()},
    **PLAYER_EVENTS,
    "SPELL_CAST_START": "开始施法", "SPELL_CAST_FAILED": "施法失败",
    "SPELL_SUMMON": "召唤", "COMBATANT_INFO": "参战角色信息",
}


def event_label(kind):
    return EVENT_LABELS.get(kind, kind)

SCHEMA = """
CREATE TABLE fight(id INTEGER PRIMARY KEY, session INTEGER, kind TEXT, game_id TEXT, name TEXT, level TEXT,
                   parent INTEGER, start_line INTEGER, end_line INTEGER, start_ms INTEGER,
                   end_ms INTEGER, result TEXT, official_ms INTEGER, affixes TEXT, challenge_id TEXT);
CREATE TABLE event(line INTEGER PRIMARY KEY, ms INTEGER, kind TEXT, src TEXT, dst TEXT,
                   target TEXT, skill TEXT, offset INTEGER, length INTEGER);
CREATE TABLE interrupt(line INTEGER PRIMARY KEY, actor TEXT, target TEXT, spell_id TEXT, spell TEXT,
                       ms INTEGER, offset INTEGER, length INTEGER);
"""


@lru_cache(maxsize=128)
def day_number(year, month, day):
    return date(year, month, day).toordinal()


def timestamp(match, fallback_year):
    month, day, year, hour, minute, second, milli, sign, offset, minutes = (
        match.groups()
    )
    base = day_number(int(year or fallback_year), int(month), int(day)) * 86400
    zone = int(offset or 0) * 3600 + int(minutes or 0) * 60
    if sign == b"-":
        zone = -zone
    return (
        base + int(hour) * 3600 + int(minute) * 60 + int(second) - zone
    ) * 1000 + int(milli)


def iter_lines(chunks):
    """保留跨分块的原始偏移；超长行跳过解析，但不丢失后续行位置。"""
    pending = bytearray()
    length = 0
    offset = 0
    for chunk in chunks:
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end + 1
            length += stop - start
            if length <= MAX_LINE:
                pending.extend(chunk[start:stop])
            if end >= 0:
                yield offset, length, bytes(pending) if length <= MAX_LINE else b""
                offset += length
                length = 0
                pending.clear()
            start = stop
    if length:
        yield offset, length, bytes(pending) if length <= MAX_LINE else b""


def session_order(chunks):
    """正式服有时先写开战和队伍信息，再写本段版本头。原文行号和偏移始终不变。"""
    opening = []
    size = 0
    for line, (offset, length, raw) in enumerate(iter_lines(chunks), 1):
        record = (line, offset, length, raw, line)
        # 常规战斗事件不复制整行来检查类型。
        prefix = raw[:96].removeprefix(b"\xef\xbb\xbf")
        stamp, _, event = prefix.partition(b"  ")
        kind = event.partition(b",")[0]
        if opening:
            if kind == b"COMBATANT_INFO" and size + length <= 4 * 1024 * 1024 and len(opening) < 256:
                opening.append(record)
                size += length
                continue
            if kind == b"COMBAT_LOG_VERSION":
                first_stamp = opening[0][3][:64].removeprefix(b"\xef\xbb\xbf").partition(b"  ")[0]
                first_match, header_match = STAMP.fullmatch(first_stamp), STAMP.fullmatch(stamp)
                try:
                    adjacent = first_match and header_match and 0 <= timestamp(header_match, 2000) - timestamp(first_match, 2000) <= 5000
                except ValueError:
                    adjacent = False
                if adjacent and b",PROJECT_ID," in raw:
                    yield (line, offset, length, raw, opening[0][0])
                    yield from opening
                    opening.clear()
                    continue
            yield from opening
            opening.clear()
        if kind == b"CHALLENGE_MODE_START":
            opening.append(record)
            size = length
        else:
            yield record
    yield from opening


class ActivityIndex:
    def __init__(self, db):
        self.db = db
        self.active = {}
        self.orphan_keys = 0
        self.session = 0

    def start(self, kind, game_id, name, level, line, ms):
        self.close(kind, line - 1, None, "终点缺失")
        parent = self.active.get("钥匙") if kind == "首领" else None
        row = self.db.execute(
            "INSERT INTO fight(session,kind,game_id,name,level,parent,start_line,start_ms,result) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.session,
                kind,
                game_id,
                name,
                level,
                parent and parent[0],
                line,
                ms,
                "终点缺失",
            ),
        )
        self.active[kind] = (row.lastrowid, game_id)

    def close(self, kind, line, ms, result, official_ms=None):
        if kind == "钥匙" and kind in self.active:
            self.close("首领", line, None, "终点缺失")
        current = self.active.pop(kind, None)
        if current:
            self.db.execute(
                "UPDATE fight SET end_line=?, end_ms=?, result=?, official_ms=? WHERE id=?",
                (line, ms, result, official_ms, current[0]),
            )

    def finish_session(self, line):
        self.close("首领", line, None, "终点缺失")
        self.close("钥匙", line, None, "终点缺失")

    def boundary(self, row, line, ms):
        kind = row[0]
        if kind == "ENCOUNTER_START":
            self.start("首领", row[1], row[2], row[3], line, ms)
        elif kind == "CHALLENGE_MODE_START":
            self.close("首领", line - 1, None, "终点缺失")
            self.start("钥匙", row[2], row[1], row[4], line, ms)
            affixes = ",".join(sorted(re.findall(r"\d+", ",".join(row[5:]))))
            self.db.execute(
                "UPDATE fight SET affixes=?,challenge_id=? WHERE id=?",
                (affixes, row[3], self.active["钥匙"][0]),
            )
        elif kind == "ENCOUNTER_END":
            result = {"1": "击杀", "0": "失败"}[row[5]]
            if self.active.get("首领", (None, None))[1] == row[1]:
                self.close("首领", line, ms, result)
            else:
                self.db.execute(
                    "INSERT INTO fight(kind,session,game_id,name,level,end_line,end_ms,result) "
                    "VALUES ('首领',?,?,?,?,?,?,?)",
                    (self.session, row[1], row[2], row[3], line, ms, result),
                )
        elif self.active.get("钥匙", (None, None))[1] == row[1]:
            self.close(
                "钥匙", line, ms, {"1": "已完成", "0": "未完成"}[row[2]], int(row[4])
            )
        else:
            self.orphan_keys += 1


def build_index(path, chunks):
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        fights = ActivityIndex(db)
        writer = facts.FactWriter(db)
        sessions = []
        supported = False
        bad = events = last_line = 0
        year, last_month = 2000, 0
        for line, offset, length, raw, session_start in session_order(chunks):
            last_line = max(last_line, line)
            stamp, separator, payload = raw.removeprefix(b"\xef\xbb\xbf").partition(
                b"  "
            )
            if not separator:
                bad += 1
                continue
            event_type = payload.partition(b",")[0].decode("ascii", "replace")
            if (
                event_type not in BOUNDARIES
                and event_type not in PLAYER_EVENTS
                and event_type not in facts.POSITION_ONLY
                and event_type not in facts.EXTRA
            ):
                continue
            try:
                match = STAMP.fullmatch(stamp)
                if not match:
                    raise ValueError
                month = int(match[1])
                if last_month == 12 and month == 1:
                    year += 1
                last_month = month
                ms = timestamp(match, year)
                row = next(csv.reader([payload.decode("utf-8").rstrip("\r\n")]))
                if supported and (event_type in combat_replay_state.AURA_ACTIONS or event_type == "UNIT_DIED"):
                    writer.auras.observe(writer, fights.session, row, line, ms)
                if event_type in BOUNDARIES:
                    writer.boundary()
                elif supported and (fights.active or event_type == "COMBATANT_INFO"):
                    writer.write(
                        row, line, ms, offset, length, fights.active, sessions[-1]
                    )
                if event_type == "COMBAT_LOG_VERSION":
                    writer.auras.active.clear()
                    writer.specs.clear()
                    writer.owners.clear()
                    fights.finish_session(session_start - 1)
                    fights.session += 1
                    fields = dict(zip(row[::2], row[1::2]))
                    supported = fields.get("PROJECT_ID") == "1"
                    sessions.append(
                        {
                            "build": fields.get("BUILD_VERSION", "未知"),
                            "format": row[1],
                            "advanced": fields.get("ADVANCED_LOG_ENABLED") == "1",
                            "retail": supported,
                        }
                    )
                elif supported and event_type in BOUNDARIES:
                    fights.boundary(row, line, ms)
                elif (
                    supported
                    and fights.active
                    and event_type == "SPELL_INTERRUPT"
                    and row[1].startswith("Player-")
                ):
                    db.execute(
                        "INSERT INTO interrupt VALUES (?,?,?,?,?,?,?,?)",
                        (line, row[2], row[6], row[12], row[13], ms, offset, length),
                    )
                elif (
                    supported
                    and fights.active
                    and event_type in PLAYER_EVENTS
                    and row[5].startswith("Player-")
                ):
                    skill = (
                        row[10]
                        if event_type.startswith(("SPELL_", "RANGE_")) and len(row) > 10
                        else ""
                    )
                    if event_type == "SPELL_ABSORBED":
                        skill = ""
                    db.execute(
                        "INSERT INTO event VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            line,
                            ms,
                            event_type,
                            row[1],
                            row[5],
                            row[6],
                            skill,
                            offset,
                            length,
                        ),
                    )
                    events += 1
            except (
                IndexError,
                KeyError,
                UnicodeError,
                csv.Error,
                ValueError,
                OverflowError,
            ):
                bad += 1
        if not any(s["retail"] for s in sessions):
            raise ValueError(
                "未找到正式服 COMBAT_LOG_VERSION 版本头，请上传游戏生成的完整文本。"
            )
        fights.finish_session(last_line)
        writer.finish_wave()
        db.executescript(facts.INDEXES + combat_replay_state.INDEXES)
        db.execute("CREATE INDEX death_lines ON event(kind,line)")
        db.execute("CREATE INDEX player_time ON event(dst,ms,line)")
        db.row_factory = sqlite3.Row
        activity_index = [
            dict(r)
            for r in db.execute("""
            SELECT f.*, (SELECT count(*) FROM event WHERE kind='UNIT_DIED'
                          AND line BETWEEN f.start_line AND f.end_line) deaths,
                   (SELECT ms FROM event WHERE kind='UNIT_DIED' AND line BETWEEN f.start_line AND f.end_line
                    ORDER BY line LIMIT 1) first_death,
                   (SELECT count(*) FROM interrupt WHERE line BETWEEN f.start_line AND f.end_line) kicks,
                   (SELECT count(*) FROM wave WHERE keystone=f.id) wave_count
            FROM fight f ORDER BY id
        """)
        ]
        facts.index_player_totals(db,activity_index)
        combat_replay_activity.index_damage(db, activity_index)
        players = dict(db.execute("SELECT guid,name FROM actor WHERE family='Player'"))
        for activity in activity_index:
            axis = "keystone" if activity["kind"] == "钥匙" else "boss"
            roster = [
                r[0]
                for r in db.execute(
                    f"SELECT guid FROM actor WHERE family='Player' AND id IN (SELECT src FROM fact WHERE {axis}=? "
                    f"UNION SELECT dst FROM fact WHERE {axis}=?) ORDER BY guid",
                    (activity["id"], activity["id"]),
                )
            ]
            activity["roster"] = (
                hashlib.sha256("\n".join(roster).encode()).hexdigest()
                if len(roster) >= 2
                else ""
            )
        db.commit()
        return {
            "lines": last_line,
            "events": events,
            "sessions": sessions,
            "bad": bad,
            "orphan_keys": fights.orphan_keys,
            "index_version": facts.VERSION,
            "position_revision": 3,
            "state_revision": 1,
            "replay_damage_revision": combat_replay_activity.DAMAGE_REVISION,
            "boundary_revision": 1,
            "unknown_amounts": writer.unknown,
            "activity_index": activity_index,
            "player_catalog": players,
            "player_totals_version": 1,
        }
    finally:
        db.close()
