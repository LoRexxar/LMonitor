"""将已选 WCL 场次映射为单场工作台读取的事件索引。"""

import json
import os
import sqlite3
import tempfile
from botend.services import combat_loading
import time
import uuid
import zlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from django.core.cache import cache
from django.core.files import locks
from django.http import Http404

from botend.models import WclUserBinding
from botend.services import combat_log_archive as archive
from botend.services import combat_log_facts as facts
from botend.services import combat_log_index, combat_replay_activity, combat_replay_state, combat_wcl_account as wcl


META = """query($code:String!,$ids:[Int]!){reportData{report(code:$code){
    code title startTime zone{id name}
    fights(fightIDs:$ids){FIELDS friendlyPlayers dungeonPulls{id name startTime endTime encounterID kill}}
    masterData{actors{id gameID name type subType petOwner} abilities{gameID name icon}}
}}}""".replace("FIELDS", wcl.FIGHT_FIELDS)
EVENTS = """query($code:String!,$ids:[Int]!,$start:Float!,$end:Float!){reportData{report(code:$code){
    events(fightIDs:$ids,startTime:$start,endTime:$end,includeResources:true,limit:10000){data nextPageTimestamp}
}}}"""
ACCESS = """query($code:String!){reportData{report(code:$code){code}}}"""
TTL = 86400
KINDS = {"cast":"SPELL_CAST_SUCCESS", "begincast":"SPELL_CAST_START", "interrupt":"SPELL_INTERRUPT",
         "dispel":"SPELL_DISPEL", "death":"UNIT_DIED", "summon":"SPELL_SUMMON",
         "damage":"SPELL_DAMAGE", "heal":"SPELL_HEAL"}
for category in ("buff", "debuff"):
    for prefix, suffix in (("apply","APPLIED"),("remove","REMOVED"),("refresh","REFRESH"),
                           ("apply","APPLIED_DOSE"),("remove","REMOVED_DOSE")):
        KINDS[prefix + category + ("stack" if suffix.endswith("DOSE") else "")] = "SPELL_AURA_" + suffix


def folder():
    return archive.directory(SimpleNamespace(pk="wcl-fights"), create=False)


def expire():
    cutoff = time.time() - TTL
    for path in folder().glob("*"):
        if path.suffix in (".sqlite3", ".partial", ".partial-journal", ".lock"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except (PermissionError, FileNotFoundError):
                continue


def read(binding, query, variables):
    report = (wcl.read(binding,query,variables).get("reportData") or {}).get("report")
    if not report:
        raise wcl.ReportUnavailable("当前 WCL 渠道无法读取这份报告。")
    return report


def events(binding, variables, fight):
    start, end = fight["startTime"], fight["endTime"]
    cursor = start
    pages = count = 0
    combat_loading.message("已取得战斗信息，正在向 WCL 请求事件。")
    while True:
        page = read(binding, EVENTS, dict(variables,start=cursor,end=end)).get("events") or {}
        data = page.get("data")
        if not isinstance(data,list) or "nextPageTimestamp" not in page:
            raise wcl.WclError("WCL 事件页不完整，请重新打开这场战斗。")
        pages += 1
        count += len(data)
        combat_loading.message(f"已收到 {pages} 页、{count:,} 条事件，正在建立索引。")
        for event in data:
            if event.get("fight") == fight["id"] and start <= event["timestamp"] <= end:
                yield event
        following = page["nextPageTimestamp"]
        if following is None:
            return
        if not cursor < following <= end:
            raise wcl.WclError("WCL 事件分页没有前进，请稍后重试。")
        cursor = following


def activity(db, ident, kind, game, label, level, parent, first, last, start, end, result, affixes=""):
    db.execute("INSERT INTO fight VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (ident,1,kind,str(game),label,str(level),parent,first,last,start,end,result,None,affixes,None))


def pull_windows(db, fight, base):
    windows = fight.get("dungeonPulls") or []
    if not windows:
        return  # 保留事实写入器依据交战空档识别的波次。
    db.execute("DELETE FROM wave")
    db.execute("UPDATE fact SET wave=0")
    for i, pull in enumerate(sorted(windows,key=lambda p:p["startTime"]),1):
        start = max(fight["startTime"], pull["startTime"]) + base
        end = min(fight["endTime"], pull["endTime"]) + base
        first,last = db.execute("SELECT MIN(line),MAX(line) FROM fact WHERE ms BETWEEN ? AND ?",(start,end)).fetchone()
        if first is None:
            continue
        if pull["encounterID"]:
            boss = fight["id"] + i
            activity(db,boss,"首领",pull["encounterID"],pull["name"],fight["difficulty"],fight["id"],
                     first,last,start,end,"击杀" if pull["kill"] else "未击杀")
            db.execute("UPDATE fact SET boss=?,wave=0 WHERE ms BETWEEN ? AND ?",(boss,start,end))
        else:
            db.execute("INSERT INTO wave VALUES (?,?,?,?,?,?)",(i,fight["id"],first,last,start,end))
            db.execute("UPDATE fact SET wave=? WHERE boss=0 AND ms BETWEEN ? AND ?",(i,start,end))
    db.execute("UPDATE opportunity SET (boss,wave)=(SELECT boss,wave FROM fact WHERE fact.line=opportunity.line)")


def wcl_integer(value):
    return value if type(value) is int and 0 < value < 2**32 else 0


def build(db, raw, fight, stream):
    db.executescript(combat_log_index.SCHEMA)
    db.execute("CREATE TABLE wcl_event(line INTEGER PRIMARY KEY,payload BLOB)")
    writer = facts.FactWriter(db)
    master = raw["masterData"]
    actors = {a["id"]:a for a in master["actors"]}
    abilities = {s["gameID"]:s["name"] for s in master["abilities"]}
    def unit(ident, instance=None):
        a = actors.get(ident) or {}
        family = {"Player":"Player","NPC":"Creature","Pet":"Pet"}.get(a.get("type"),"Unknown")
        if a.get("petOwner"):
            family = "Pet"
        if family == "Player":
            guid = f"Player-WCL-{raw['code']}-{ident}"
        else:
            guid = f"{family}-0-0-0-0-{a.get('gameID') or 0}-{ident or 0}.{instance or 1}"
        return guid, a.get("name") or "未知单位"

    db.execute("CREATE TABLE wcl_class(guid TEXT PRIMARY KEY, class_name TEXT)")
    db.executemany("INSERT OR IGNORE INTO wcl_class VALUES (?,?)", (
        (unit(a["id"])[0], a["subType"]) for a in actors.values() if a.get("type") == "Player" and a.get("subType")))
    db.execute("CREATE TABLE wcl_icon(spell INTEGER PRIMARY KEY, icon TEXT)")
    db.executemany("INSERT OR IGNORE INTO wcl_icon VALUES (?,?)", (
        (s["gameID"], s["icon"]) for s in master["abilities"] if s.get("icon")))
    kind = "钥匙" if fight["dungeon"] else "首领"
    active = {kind:(fight["id"],)}
    session = {"advanced":False,"format":"WCL"}
    last = 0
    for e in stream:
        typ = e["type"]
        src, source = unit(e.get("sourceID"),e.get("sourceInstance"))
        dst, target = unit(e.get("targetID"),e.get("targetInstance"))
        if typ == "combatantinfo":
            if isinstance(e.get("talentTree"), list):
                entries = [[t["id"], t["rank"], wcl_integer(t.get("nodeID")), wcl_integer(t.get("spellID"))] for t in e["talentTree"]
                           if isinstance(t, dict) and type(t.get("id")) is int and 0 < t["id"] < 2**32
                           and type(t.get("rank")) is int and 0 < t["rank"] < 256]
                writer.record_build(1, src, last+1, e.get("specID") or 0, entries)
            if e.get("specID") in writer.known_specs:
                writer.specs[src] = e["specID"]
            continue
        sid = e.get("abilityGameID",0)
        if typ not in KINDS or e.get("fake") or (typ == "cast" and sid == 1):
            continue
        event_kind = KINDS[typ]
        if typ == "damage" and sid == 1:
            event_kind = "SWING_DAMAGE"
        elif typ in ("damage","heal") and e.get("tick"):
            event_kind = "SPELL_PERIODIC_" + typ.upper()
        row = [event_kind,src,source,"","",dst,target,"","",str(sid),abilities.get(sid,str(sid)),""]
        if typ in ("interrupt","dispel"):
            extra = e.get("extraAbilityGameID") or 0
            row.extend([str(extra),abilities.get(extra,str(extra))])
        elif "buff" in typ:
            row.extend(["DEBUFF" if "debuff" in typ else "BUFF",str(e.get("stack",1))])
        owner = (actors.get(e.get("sourceID")) or {}).get("petOwner")
        if owner:
            owner_guid, owner_name = unit(owner)
            writer.actor(owner_guid,owner_name)
            writer.owners[src] = owner_guid
        last += 1
        ms = raw["startTime"] + e["timestamp"]
        hp, maximum = e.get("hitPoints"),e.get("maxHitPoints")
        valid_hp = maximum and hp is not None and 0 <= hp <= maximum and e.get("resourceActor") in (1,2)
        target_hp = valid_hp and e["resourceActor"] == 2
        measured = (e.get("amount"), e.get("overheal",0) if typ=="heal" else e.get("overkill",0),
                    hp if target_hp else None,maximum if target_hp else None)
        payload = json.dumps(e,ensure_ascii=False,separators=(",",":")).encode()
        writer.write(row,last,ms,0,len(payload),active,session,measured=measured)
        writer.auras.observe(writer,1,row,last,ms)
        if valid_hp:
            actor = writer.actors[src if e["resourceActor"] == 1 else dst]
            db.execute("INSERT INTO replay_hp VALUES (?,?,?,?,?)",(last,actor,ms,hp,maximum))
        db.execute("INSERT INTO wcl_event VALUES (?,?)",(last,zlib.compress(payload,1)))
    if not last:
        raise wcl.WclError("这场战斗没有可读取的事件。")
    writer.finish_wave()
    base = raw["startTime"]
    result = ("已完成" if fight["dungeon"] else "击杀") if fight["kill"] else ("未完成" if fight["dungeon"] else "未击杀")
    activity(db,fight["id"],kind,fight["encounterID"],fight["label"],
             (fight["keystoneLevel"] or "未知") if fight["dungeon"] else fight["difficulty"],0,
             1,last,base+fight["startTime"],base+fight["endTime"],result if fight["kill"] is not None else "结果未知",
             ",".join(map(str,fight.get("keystoneAffixes") or [])))
    if fight["dungeon"]:
        pull_windows(db,fight,base)
    db.executescript(facts.INDEXES + combat_replay_state.INDEXES)
    fights = db.cursor()
    fights.row_factory = sqlite3.Row
    combat_replay_activity.index_damage(db, fights.execute("SELECT * FROM fight"))


def index_key(user, binding, code, fid):
    account = binding.wcl_id if binding else None
    return uuid.uuid5(uuid.NAMESPACE_URL,f"lmonitor/wcl/1/{user.pk}/{account}/{code}/{fid}")


def current(path):
    try:
        if path.stat().st_mtime <= time.time() - TTL:
            return False
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
            return db.execute("PRAGMA user_version").fetchone()[0] == 2
    except (FileNotFoundError, sqlite3.DatabaseError):
        return False


def materialize(user, binding, code, fid):
    key = index_key(user, binding, code, fid)
    root = folder()
    root.mkdir(parents=True,exist_ok=True)
    path = root / f"{key}.sqlite3"
    if current(path):
        return key
    with path.with_suffix(".lock").open("ab") as guard:
        if not locks.lock(guard,locks.LOCK_EX | locks.LOCK_NB):
            raise wcl.WclError("这场战斗正在建立事件索引，请稍后重新打开。")
        if not current(path):
            os.utime(guard.name,None)
            write_index(user,binding,code,fid,path)
            cache.set(f"wcl-view:{key}",True,30)
    return key


def write_index(user, binding, code, fid, path):
    from botend.services.combat_wcl_catalog import summarize
    variables = dict(code=code,ids=[fid])
    combat_loading.message("正在读取本场战斗的角色和技能资料。")
    raw = read(binding,META,variables)
    if not raw.get("masterData"):
        raise wcl.WclError("当前报告的角色和技能资料暂不可用。")
    if not any(f["id"] == fid for f in raw.get("fights") or []):
        raise wcl.ReportUnavailable("当前报告中找不到所选战斗。")
    summarize([raw])
    fight = next(f for f in raw["fights"] if f["id"] == fid)
    with tempfile.NamedTemporaryFile(dir=path.parent,suffix=".partial",delete=False) as tmp:
        staging = Path(tmp.name)
    try:
        with closing(sqlite3.connect(staging)) as db:
            build(db,raw,fight,events(binding,variables,fight))
            meta = dict(user=user.pk, account=binding.wcl_id if binding else None, code=code, fight=fid, focus=fight["key"],
                        title=raw["title_label"], talent_revision=1, revision=str(uuid.uuid4()),
                        replay_damage_revision=combat_replay_activity.DAMAGE_REVISION)
            db.execute("PRAGMA user_version=2")
            db.execute("CREATE TABLE wcl_source(meta TEXT)")
            db.execute("INSERT INTO wcl_source VALUES (?)",(json.dumps(meta),))
            db.commit()
        os.replace(staging,path)
    finally:
        staging.unlink(missing_ok=True)


def needs_access_check(key):
    return (folder() / f"{key}.sqlite3").is_file() and not cache.get(f"wcl-view:{key}")


def resolve(user, key):
    path = folder() / f"{uuid.UUID(str(key))}.sqlite3"
    if not path.is_file() or path.stat().st_mtime < time.time()-TTL:
        return None
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as db:
        meta = json.loads(db.execute("SELECT meta FROM wcl_source").fetchone()[0])
    if meta["user"] != user.pk:
        raise Http404
    binding = None
    if meta["account"] is not None:
        binding = WclUserBinding.objects.filter(user=user,wcl_id=meta["account"]).first()
        if not binding:
            raise Http404
    if not cache.get(f"wcl-view:{key}"):
        read(binding,ACCESS,{"code":meta["code"]})
        cache.set(f"wcl-view:{key}",True,30)
    return SimpleNamespace(pk=key, published=meta["revision"], filename=meta["title"], digest=str(key),
                           wcl_source=meta, wcl_index=path, owner_id=None,
                           summary={"index_version":facts.VERSION,"sessions":[{"build":"WCL","format":"WCL","advanced":False}],
                                    "position_revision":3,"state_revision":1,"talent_revision":meta.get("talent_revision"),
                                    "replay_damage_revision":meta.get("replay_damage_revision")})
