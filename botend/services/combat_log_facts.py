"""正式服事件事实、敌方读条机会和钥匙内的交战波次。"""

import math

from botend.constants.wow import SPEC_ROLE, specialization_catalog
from botend.services import combat_replay_state

VERSION = 3


def _clock_label(ms):
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def pull_catalog(db, fight, *, include_enemies=False):
    """小怪用原波次编号，首领用负场次编号，分析和回放共用边界。"""
    if fight["kind"] != "钥匙":
        return []
    enemies = "NULL"
    args = []
    if include_enemies:
        args.extend(sorted(DAMAGE))
        markers = ",".join("?" for _ in args)
        enemies = f"""(SELECT group_concat(name,'、') FROM (
            SELECT a.name FROM fact f JOIN actor s ON s.id=f.credited JOIN actor d ON d.id=f.dst
            JOIN actor a ON a.id=CASE WHEN s.family IN ('Player','Pet') THEN f.dst ELSE f.src END
            WHERE f.wave=w.id AND f.line BETWEEN w.first_line AND w.last_line AND COALESCE(f.boss,0)=0
            AND f.kind IN ({markers}) AND (s.family IN ('Player','Pet') OR d.family IN ('Player','Pet'))
            AND a.family IN ('Creature','Vehicle') GROUP BY a.name ORDER BY MIN(f.line) LIMIT 3
        ))"""
    rows = db.execute(f"""
        SELECT id,first_line,last_line,start_ms,end_ms,NULL name,1 known,{enemies} enemies FROM wave w WHERE keystone=?
        UNION ALL SELECT -b.id,b.start_line,b.end_line,b.start_ms,
            COALESCE(b.end_ms,(SELECT MAX(ms) FROM fact WHERE boss=b.id),b.start_ms),
            b.name,b.end_ms IS NOT NULL,NULL
        FROM fight b WHERE b.parent=? AND b.start_ms IS NOT NULL ORDER BY first_line
    """, (*args, fight["id"], fight["id"]))
    result = []
    number = 0
    for row in rows:
        pull = dict(row)
        start = max(fight["start_ms"],pull["start_ms"])
        stop = pull["end_ms"]
        if fight["end_ms"] is not None:
            stop = min(stop,fight["end_ms"])
        if start > stop:
            continue
        if pull["id"] > 0:
            number += 1
            label = f"第 {number} 波"
        else:
            label = pull["name"]
        t, end = start-fight["start_ms"],stop-fight["start_ms"]
        pull.update(start_ms=start,end_ms=stop,t=t,end=end,label=label,
                    caption=f"{'首领 · ' if pull['id'] < 0 else ''}{label}　{_clock_label(t)}–{_clock_label(end)}")
        result.append(pull)
    return result


DAMAGE = {
    "SPELL_DAMAGE",
    "SPELL_PERIODIC_DAMAGE",
    "RANGE_DAMAGE",
    "SWING_DAMAGE",
    "ENVIRONMENTAL_DAMAGE",
    "DAMAGE_SHIELD",
    "DAMAGE_SPLIT",
}
HEAL = {"SPELL_HEAL", "SPELL_PERIODIC_HEAL"}
POSITION_ONLY = {"SWING_DAMAGE_LANDED", "SPELL_ENERGIZE", "SPELL_PERIODIC_ENERGIZE", "SPELL_DRAIN", "SPELL_LEECH"}
EXTRA = {
    "SPELL_CAST_START",
    "SPELL_CAST_FAILED",
    *combat_replay_state.AURA_ACTIONS,
    "SPELL_AURA_APPLIED_DOSE",
    "COMBATANT_INFO",
    "SPELL_SUMMON",
}
SCHEMA = """
CREATE TABLE actor(id INTEGER PRIMARY KEY, guid TEXT UNIQUE, name TEXT, family TEXT);
CREATE TABLE ability(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE fact(line INTEGER PRIMARY KEY, ms INTEGER, boss INTEGER, keystone INTEGER, wave INTEGER,
                  kind TEXT, src INTEGER, dst INTEGER, spell TEXT, amount INTEGER, excess INTEGER,
                  hp INTEGER, max_hp INTEGER, offset INTEGER, length INTEGER, extra_spell TEXT,
                  credited INTEGER, src_spec INTEGER, dst_spec INTEGER, aura_type TEXT, aura_stacks INTEGER);
CREATE TABLE spec(id INTEGER PRIMARY KEY,label TEXT,role TEXT);
CREATE TABLE wave(id INTEGER PRIMARY KEY, keystone INTEGER, first_line INTEGER, last_line INTEGER,
                  start_ms INTEGER, end_ms INTEGER);
CREATE TABLE opportunity(line INTEGER PRIMARY KEY, boss INTEGER, keystone INTEGER, wave INTEGER,
                         enemy INTEGER, spell TEXT, outcome TEXT, response_line INTEGER);
CREATE TABLE sample(line INTEGER PRIMARY KEY, actor INTEGER, ms INTEGER, hp INTEGER, maximum INTEGER,
                    map INTEGER,x REAL,y REAL,power_type INTEGER,power INTEGER,power_max INTEGER, facing REAL);
"""
INDEXES = """
CREATE INDEX fact_boss_time ON fact(boss,ms,line);
CREATE INDEX fact_key_time ON fact(keystone,ms,line);
CREATE INDEX fact_wave ON fact(wave,line);
CREATE INDEX opportunity_boss ON opportunity(boss,line);
CREATE INDEX opportunity_key ON opportunity(keystone,line);
CREATE INDEX fact_skill_line ON fact(spell,line);
CREATE INDEX sample_time ON sample(ms,line);
CREATE INDEX sample_actor_line ON sample(actor,line);
CREATE INDEX fact_src_line ON fact(src,line);
CREATE INDEX fact_dst_line ON fact(dst,line);
"""


def index_player_totals(db, activities):
    """按已确认的召唤归属预聚合整场速率的分子和数值完整性。"""
    db.execute("""CREATE TABLE player_total(
        fight INTEGER,metric TEXT,guid TEXT,amount INTEGER,hits INTEGER,known INTEGER,
        PRIMARY KEY(fight,metric,guid)) WITHOUT ROWID""")
    for activity in activities:
        if activity["start_ms"] is None or activity["end_ms"] is None:
            continue
        axis = "keystone" if activity["kind"] == "钥匙" else "boss"
        for metric, kinds in (("damage", DAMAGE), ("heal", HEAL)):
            markers = ",".join("?" for _ in kinds)
            db.execute(
                f"""INSERT INTO player_total
                SELECT f.{axis},?,s.guid,SUM(f.amount),COUNT(*),COUNT(f.amount)
                FROM fact f JOIN actor s ON s.id=f.credited
                WHERE f.{axis}=? AND f.ms>=? AND s.family='Player' AND f.kind IN ({markers})
                GROUP BY s.guid""",
                (metric, activity["id"], activity["start_ms"], *kinds),
            )


def values(row, advanced, version):
    """只接受明确的伤害／治疗尾部布局。普通攻击命中镜像不进入事实。"""
    kind = row[0]
    start = 9 if kind in ("SWING_DAMAGE", "ENVIRONMENTAL_DAMAGE") else 12
    hp = maximum = None
    if kind not in DAMAGE | HEAL or version not in ("16", "22"):
        return None, None, hp, maximum
    env = kind == "ENVIRONMENTAL_DAMAGE"
    tail = len(row) - (start + int(env))
    widths = (17,) if version == "16" else (19, 17)
    for width in widths if advanced else (0,):
        fields = tail - width
        if kind in HEAL:
            if fields not in (4, 5):
                continue
        elif fields not in (9, 10, 11):
            continue
        pos = start + width + int(env)
        suffix = row[pos:]
        if width and not (
            row[start].startswith(("Player-", "Creature-", "Pet-", "Vehicle-", "GameObject-"))
            or row[start].strip("0") == ""
        ):
            continue
        if kind in DAMAGE and suffix[-1] in ("ST", "AOE"):
            suffix = suffix[:-1]
        expected = 5 if kind in HEAL else 10
        if len(suffix) not in (expected, expected - 1):
            continue
        try:
            amount = int(suffix[0])
            excess = max(0, int(suffix[2 if len(suffix) == expected else 1]))
            if width and row[start] == row[5]:
                hp, maximum = int(row[start + 2]), int(row[start + 3])
            if not 0 <= amount < 2**60 or excess > amount:
                continue
            return amount - excess, excess, hp, maximum
        except (ValueError, IndexError):
            continue
    return None, None, None, None


class FactWriter:
    def __init__(self, db):
        self.db = db
        db.executescript(SCHEMA + combat_replay_state.SCHEMA)
        self.auras = combat_replay_state.AuraWriter()
        self.actors = {}
        self.spells = set()
        self.pending = {}
        self.owners = {}
        self.specs = {}
        self.known_specs = set()
        db.execute("INSERT INTO spec VALUES (0,'未知专精','未知职责')")
        for item in specialization_catalog():
            role = SPEC_ROLE.get((item["class_name"], item["spec_name"]))
            self.known_specs.add(item["spec_id"])
            db.execute(
                "INSERT INTO spec VALUES (?,?,?)",
                (
                    item["spec_id"],
                    item["label"],
                    {"tank": "坦克", "healer": "治疗", "dps": "输出"}.get(role, "未知职责"),
                ),
            )
        self.wave = None
        self.unknown = 0

    def actor(self, guid, name):
        if guid not in self.actors:
            actor = len(self.actors) + 1
            family = guid.partition("-")[0]
            self.db.execute("INSERT INTO actor VALUES (?,?,?,?)", (actor, guid, name, family))
            self.actors[guid] = actor
        return self.actors[guid]

    def spell(self, key, name):
        if key not in self.spells:
            self.db.execute("INSERT INTO ability VALUES (?,?)", (key, name))
            self.spells.add(key)
        return key

    def boundary(self):
        self.pending.clear()
        self.finish_wave()

    def finish_wave(self):
        if self.wave:
            self.db.execute("INSERT INTO wave VALUES (?,?,?,?,?,?)", self.wave)
            self.wave = None

    def snapshot(self, row, session, line, ms, src, dst):
        if not session["advanced"] or session["format"] not in ("16", "20", "22"):
            return
        start = 9 if row[0].startswith("SWING_") or row[0] == "ENVIRONMENTAL_DAMAGE" else 12
        if row[0] == "ENVIRONMENTAL_DAMAGE" and len(row) > 10 and row[10] == row[5]:
            start = 10
        if len(row) <= start or row[start] not in (row[1], row[5]) or not row[start].startswith(("Player-", "Creature-", "Pet-", "Vehicle-")):
            return
        candidates = []
        widths = (19, 17) if session["format"] == "22" else (17,)
        for width in widths:
            end = start + width
            try:
                x, y = float(row[end - 5]), float(row[end - 4])
                facing = float(row[end - 2])
                hp, maximum = int(row[start + 2]), int(row[start + 3])
                map_id = int(row[end - 3])
                int(row[end - 1])
                if not all(math.isfinite(v) for v in (x, y, facing)) or not 0 <= facing <= math.tau + 0.001:
                    continue
                if map_id <= 0 or not 0 <= hp <= maximum or maximum <= 0:
                    continue
                power = [int(v) if v.lstrip("-").isdigit() else None for v in row[end - 9 : end - 6]]
            except (IndexError, ValueError):
                continue
            # 兼容带旧布局的 22 版会话；伤害尾部的低整数也可能恰好是有效朝向。
            score = sum("." in row[end - n] for n in (5, 4, 2))
            point = (line, src if row[start] == row[1] else dst, ms, hp, maximum,
                     map_id, x, y, *power, facing)
            candidates.append((score, point))
        if candidates:
            _, point = max(candidates, key=lambda candidate: candidate[0])
            self.db.execute("INSERT INTO sample VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", point)
        elif len(row) >= start + 17:
            # 高级头中的生命值属于 infoGUID，即使没有可用的地图或坐标也可独立读取。
            try:
                hp, maximum = int(row[start+2]), int(row[start+3])
            except ValueError:
                return
            if 0 <= hp <= maximum < 2**63 and maximum > 0:
                actor = src if row[start] == row[1] else dst
                self.db.execute("INSERT INTO replay_hp VALUES (?,?,?,?,?)", (line, actor, ms, hp, maximum))

    def write(self, row, line, ms, offset, length, active, session, *, measured=None):
        if row[0] == "COMBATANT_INFO":
            boundary = next((i for i, v in enumerate(row[3:], 3) if v.startswith(("(", "["))), None)
            if boundary and row[boundary - 1].isdigit():
                ident = int(row[boundary - 1])
                self.specs[row[1]] = ident if ident in self.known_specs else 0
            return
        if len(row) < 9:
            return
        boss = active.get("首领", (0,))[0]
        key = active.get("钥匙", (0,))[0]
        kind = row[0]
        src, dst = self.actor(row[1], row[2]), self.actor(row[5], row[6])
        if kind in POSITION_ONLY:
            self.snapshot(row, session, line, ms, src, dst)
            return
        if (
            kind == "SPELL_SUMMON"
            and row[1].startswith("Player-")
            and row[5].startswith(("Pet-", "Creature-"))
        ):
            self.owners[row[5]] = row[1]
        pos = 9 if kind in ("SWING_DAMAGE", "ENVIRONMENTAL_DAMAGE") else 12
        if len(row) > pos + 1 and row[pos].startswith("Pet-") and row[pos + 1].startswith("Player-"):
            self.owners[row[pos]] = row[pos + 1]
        owner = self.owners.get(row[1])
        credit = self.actors.get(owner, src)
        self.snapshot(row, session, line, ms, src, dst)
        named = kind.startswith(("SPELL_", "RANGE_", "DAMAGE_")) and len(row) >= 12 and row[9].isdigit()
        spell = (
            self.spell(row[9], row[10])
            if named
            else self.spell(
                kind,
                {"SWING_DAMAGE": "近战攻击", "ENVIRONMENTAL_DAMAGE": "环境伤害", "UNIT_DIED": "死亡"}.get(
                    kind, kind
                ),
            )
        )
        amount, excess, hp, maximum = measured if measured is not None else values(row, session["advanced"], session["format"])
        if amount is None and kind in DAMAGE | HEAL:
            self.unknown += 1
        # 用双方直接发生的伤害划定小怪交战；空档和首领边界分别封段。
        player = ("Player-", "Pet-")
        enemy = ("Creature-", "Vehicle-")
        contact = kind in DAMAGE and (
            (row[1].startswith(player) and row[5].startswith(enemy))
            or (row[5].startswith(player) and row[1].startswith(enemy))
        )
        if self.wave and (ms - self.wave[-1] > 8000 or ms < self.wave[-1]):
            self.finish_wave()
        if key and not boss and contact:
            if not self.wave:
                self.wave = [line, key, line, line, ms, ms]
            self.wave[3], self.wave[5] = line, ms
        wave = self.wave[0] if self.wave else 0
        extra = row[12] if kind in ("SPELL_DISPEL", "SPELL_INTERRUPT") and len(row) > 13 else None
        if extra and extra.isdigit():
            self.spell(extra, row[13])
        self.db.execute(
            "INSERT INTO fact VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                line,
                ms,
                boss,
                key,
                wave,
                kind,
                src,
                dst,
                spell,
                amount,
                excess,
                hp,
                maximum,
                offset,
                length,
                extra,
                credit,
                self.specs.get(owner or row[1], 0),
                self.specs.get(row[5], 0),
                *combat_replay_state.aura_fields(row),
            ),
        )
        if kind == "SPELL_CAST_START" and row[1].startswith(enemy):
            self.pending[src] = (line, ms, spell)
            self.db.execute(
                "INSERT INTO opportunity VALUES (?,?,?,?,?,?,?,NULL)",
                (line, boss, key, wave, src, spell, "未知"),
            )
        elif kind == "SPELL_INTERRUPT" and len(row) >= 14:
            self.resolve(dst, self.spell(row[12], row[13]), ms, line, "打断")
        elif kind in ("SPELL_CAST_SUCCESS", "SPELL_CAST_FAILED"):
            self.resolve(src, spell, ms, line, "完成" if kind == "SPELL_CAST_SUCCESS" else "失败")

    def resolve(self, actor, spell, ms, line, outcome):
        record = self.pending.get(actor)
        if record and record[2] == spell and 0 <= ms - record[1] <= 30000:
            self.db.execute(
                "UPDATE opportunity SET outcome=?,response_line=? WHERE line=?", (outcome, line, record[0])
            )
            del self.pending[actor]
