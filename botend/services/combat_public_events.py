"""按公开排行的报告、场次和角色身份读取完整战斗事件。"""

import json
import re
import time
from types import SimpleNamespace

from django.core.cache import cache

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.services import combat_log_archive
from botend.services.combat_log_duel import DETAILS
from botend.services.raid_burst_events import realm_key
from botend.services.raid_burst_snapshot import digest, read_json, write_json


META = """query($code:String!,$ids:[Int]) {reportData{report(code:$code){
 fights(fightIDs:$ids){id encounterID keystoneLevel difficulty friendlyPlayers startTime endTime}
 masterData{actors{id name server type subType petOwner} abilities{gameID name}}
}}}"""
EVENTS = """query($code:String!,$ids:[Int],$expr:String!,$start:Float!,$end:Float!){
 reportData{report(code:$code){events(fightIDs:$ids,filterExpression:$expr,
 startTime:$start,endTime:$end,limit:10000){data nextPageTimestamp}}}}"""


class Unavailable(ValueError):
    pass


CACHE_TTL = 86400


def expire_snapshots():
    folder = combat_log_archive.directory(SimpleNamespace(pk="public-benchmarks"), create=False)
    if folder.is_symlink() or folder.resolve() != folder:
        return
    cutoff = time.time() - CACHE_TTL
    for path in folder.glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{64}\.json|\.pending-[\w-]+\.json", path.name):
            continue
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except (FileNotFoundError, PermissionError):
            continue


def report_data(client, query, variables):
    data = client._wcl_graphql(query, variables)
    report = ((data or {}).get("reportData") or {}).get("report")
    if not report:
        raise Unavailable("暂时无法读取 WCL 战斗明细，请稍后重试。")
    return report


def accumulate(metrics, responses, event, actor, owned):
    kind = event["type"]
    if event.get("fake"):
        return
    outgoing = event.get("sourceID") in owned
    incoming = event.get("targetID") == actor
    spell = str(event.get("abilityGameID") or 0)
    if kind == "cast" and spell == "1":
        # WCL 为自动近战生成施法条目，本地成功施法不含自动攻击。
        return
    if spell == "1" and kind == "damage":
        spell = "SWING_DAMAGE"

    def add(key, amount):
        if not isinstance(amount, int) or amount < 0:
            raise Unavailable("公开事件缺少可核对的数值。")
        metrics[key][spell] = metrics[key].get(spell, 0) + amount

    if kind == "damage" and (outgoing or incoming):
        # WCL 的 amount 为有效数值，过量部分使用独立字段。
        if outgoing:
            add("damage", event["amount"])
        if incoming:
            add("taken", event["amount"])
    elif kind == "heal" and outgoing:
        add("heal", event["amount"])
        add("overheal", event.get("overheal", 0))
    elif outgoing and kind in ("cast", "interrupt", "dispel"):
        key = {"cast": "casts", "interrupt": "interrupts", "dispel": "dispels"}[kind]
        add(key, 1)
        if key in responses:
            extra = str(event.get("extraAbilityGameID") or "unknown")
            responses[key][extra] = responses[key].get(extra, 0) + 1
    elif incoming and kind == "death":
        spell = "UNIT_DIED"
        add("deaths", 1)


def fetch(ref, client):
    variables = {"code": ref["report_code"], "ids": [ref["fight_id"]]}
    report = report_data(client, META, variables)
    fights = [f for f in report["fights"] if f["id"] == ref["fight_id"]
              and f["encounterID"] == ref["encounter_id"]
              and f[ref["level_field"]] == ref["level"]]
    if len(fights) != 1:
        raise Unavailable("公开战斗的目标、层数或难度与排行记录不符。")
    fight = fights[0]
    actors = report["masterData"]["actors"]
    players = [a for a in actors if a["type"] == "Player" and a["id"] in fight["friendlyPlayers"]
               and a["name"].casefold() == ref["name"].casefold() and a["subType"] == ref["class_name"]
               and (not ref["realm"] or realm_key(a["server"]) == realm_key(ref["realm"]))]
    if len(players) != 1:
        raise Unavailable("无法在公开场次中唯一确认标杆角色。")
    player = players[0]
    owned = {player["id"]}
    while descendants := {a["id"] for a in actors if a.get("petOwner") in owned} - owned:
        owned.update(descendants)
    names = sorted({a["name"] for a in actors if a["id"] in owned})
    source_names = ",".join(json.dumps(name, ensure_ascii=False) for name in names)
    target_name = json.dumps(player["name"], ensure_ascii=False)
    expr = ("type in ('damage','heal','cast','interrupt','dispel','death') and "
            f"(source.name in ({source_names}) or target.name = {target_name})")
    metrics = {key: {} for key in DETAILS}
    responses = {"interrupts": {}, "dispels": {}}
    start, end = fight["startTime"], fight["endTime"]
    if end <= start:
        raise Unavailable("公开战斗没有完整的时间边界。")
    cursor = start
    while True:
        page = report_data(client, EVENTS, dict(variables, expr=expr, start=cursor, end=end))["events"]
        for event in page["data"]:
            if event["fight"] == fight["id"] and start <= event["timestamp"] <= end:
                accumulate(metrics, responses, event, player["id"], owned)
        following = page["nextPageTimestamp"]
        if following is None:
            break
        if not cursor < following <= end:
            raise Unavailable("公开事件分页边界异常，请稍后重试。")
        cursor = following
    if not metrics["damage"]:
        raise Unavailable("公开战斗未返回该排行角色的伤害事件。")
    ids = set().union(*metrics.values(), *responses.values())
    names = {str(a["gameID"]): a["name"] for a in report["masterData"]["abilities"]
             if str(a["gameID"]) in ids}
    names.update(SWING_DAMAGE="近战攻击", UNIT_DIED="死亡")
    return dict(metrics=metrics, responses=responses, names=names, seconds=(end - start) / 1000)


def snapshot(sample, ref, *, load):
    identity = {k: ref[k] for k in ("report_code", "fight_id", "encounter_id", "level_field",
                                     "level", "name", "realm", "class_name", "spec")}
    key = digest([3, identity])
    root = combat_log_archive.directory(sample["report"], create=False).parent
    path = root / "public-benchmarks" / (key + ".json")
    saved = read_json(path)
    if saved and saved["expires"] > time.time():
        return saved["detail"], ""
    if not load:
        return None, ""
    lock = "combat-public-" + key
    if not cache.add(lock, True, 180):
        return None, "这份公开明细正在读取，请稍后重试。"
    try:
        detail = fetch(ref, SpecDetailBase(None, None))
        write_json(path, {"expires": time.time() + CACHE_TTL, "detail": detail})
        return detail, ""
    except Unavailable as exc:
        return None, str(exc)
    except (KeyError, TypeError, OSError):
        return None, "公开明细读取不完整，请稍后重试。"
    finally:
        cache.delete(lock)
