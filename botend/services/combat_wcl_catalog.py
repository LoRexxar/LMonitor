"""WCL 报告的战斗目录、同首领归组及不同记录视角的合并。"""

from collections import defaultdict
from datetime import datetime, timezone
import re

from botend.constants.wow import DUNGEON_CN, RAID_BOSS_CN, RAID_ZONE_CN
from botend.services import combat_log_localization as localization
from botend.services import combat_wcl_account as wcl


DIFFICULTY = {1: "随机团队", 2: "弹性", 3: "普通", 4: "英雄", 5: "史诗"}


def group_key(report, fight):
    affixes = ".".join(str(v) for v in sorted(fight.get("keystoneAffixes") or []))
    return "-".join(str(v or 0) for v in (
        (report.get("zone") or {}).get("id"), fight["encounterID"], fight["difficulty"],
        fight.get("keystoneLevel"), affixes,
    ))


def summarize(reports):
    bosses = {f["encounterID"] for r in reports for f in r.get("fights") or [] if not f.get("keystoneLevel")}
    localized = localization._fight_names([dict(id=i, kind="首领", game_id=i) for i in bosses])
    for r in reports:
        groups = {}
        r["date"] = datetime.fromtimestamp(r["startTime"]/1000, timezone.utc)
        zone = (r.get("zone") or {}).get("name", "")
        r["zone_label"] = RAID_ZONE_CN.get(zone) or DUNGEON_CN.get(zone) or zone
        season = re.fullmatch(r"Mythic\+ Season (\d+)", zone)
        if season:
            r["zone_label"] = f"大秘境第 {season[1]} 赛季"
        r["title_label"] = r["zone_label"] if r["title"] == zone else r["title"]
        for f in r.get("fights") or []:
            key = group_key(r, f)
            level = f.get("keystoneLevel")
            dungeon = bool(level or (f["difficulty"] == 10 and (season or f["name"] in DUNGEON_CN)))
            fallback = (DUNGEON_CN if dungeon else RAID_BOSS_CN).get(f["name"], f["name"])
            label = fallback if dungeon else localized.get(f["encounterID"], fallback)
            if dungeon:
                difficulty = f"大秘境 +{level}" if level else "大秘境 · 层数未知"
            else:
                difficulty = DIFFICULTY.get(f["difficulty"], f"难度 {f['difficulty']}")
            f.update(key=key, label=label, difficulty_label=difficulty, dungeon=dungeon)
            group = groups.setdefault(key, dict(key=key,name=label,difficulty=difficulty,level=level,dungeon=dungeon,
                                                attempts=0,kills=0,wipes=0,best=None))
            group["attempts"] += 1
            group["kills"] += int(f["kill"] is True)
            group["wipes"] += int(f["kill"] is False)
            pct = f["fightPercentage"]
            if pct is not None and (group["best"] is None or pct < group["best"]):
                group["best"] = pct
        r["groups"] = sorted(groups.values(), key=lambda g:-g["attempts"])
        r["attempts"] = sum(g["attempts"] for g in r["groups"])
        r["keys"] = set(groups)


def fetch_reports(codes, binding):
    # 一次请求获取所有选中报告的目录；只有选择具体场次后才拉取输出和死亡事件。
    fields = wcl.REPORT_FIELDS + " fights(killType:Encounters){" + wcl.FIGHT_FIELDS + " friendlyPlayers}"
    if len(codes) > 1:
        fields += ' masterData{actors(type:"Player"){id name server}}'
    args = ",".join(f"$c{i}:String!" for i in range(len(codes)))
    selection = " ".join(f"r{i}:report(code:$c{i}){{{fields}}}" for i in range(len(codes)))
    data = wcl.read(binding, f"query({args}){{reportData{{{selection}}}}}",
                    {f"c{i}":code for i,code in enumerate(codes)})["reportData"]
    reports = [data.get(f"r{i}") for i in range(len(codes))]
    if not all(reports):
        raise wcl.ReportUnavailable("所选报告中有记录不存在或当前账号无权读取。")
    summarize(reports)
    return reports


def _same_pull(a, b):
    if a["code"] == b["code"] or a["kill"] != b["kill"]:
        return False
    if abs(a["absolute_start"]-b["absolute_start"])>5000 or abs(a["absolute_end"]-b["absolute_end"])>5000:
        return False
    shared = len(a["roster"] & b["roster"])
    size = max(len(a["roster"] | b["roster"]), len(a["friendlyPlayers"] or []), len(b["friendlyPlayers"] or []))
    return shared >= 3 and shared >= .8 * size


def attempts(reports):
    all_groups = defaultdict(list)
    for r in reports:
        actors = {a["id"]:(a["name"].casefold(), a["server"].casefold())
                  for a in (r.get("masterData") or {}).get("actors", []) if a.get("name") and a.get("server")}
        for f in r["fights"] or []:
            start, end = f["startTime"], f["endTime"]
            f.update(code=r["code"], ref=f"{r['code']}.{f['id']}", owner=(r.get("owner") or {}).get("name"),
                     absolute_start=r["startTime"]+start, absolute_end=r["startTime"]+end,
                     duration=round((end-start)/1000,1), date=datetime.fromtimestamp((r["startTime"]+start)/1000,timezone.utc),
                     roster={actors[i] for i in f["friendlyPlayers"] or [] if i in actors})
            all_groups[f["key"]].append(f)
    groups = []
    order = {r["code"]:n for n,r in enumerate(reports)}
    for key, raw in all_groups.items():
        matches = []
        for fight in sorted(raw,key=lambda f:f["absolute_start"]):
            match = None
            for views in reversed(matches):
                if fight["absolute_start"] - views[0]["absolute_start"] > 5000:
                    break
                if any(v["code"] == fight["code"] for v in views):
                    continue
                if _same_pull(fight,views[0]):
                    match = views
                    break
            if match is None:
                matches.append([fight])
            else:
                match.append(fight)
        pulls = []
        for i,views in enumerate(matches,1):
            f = min(views,key=lambda v:order[v["code"]])
            pulls.append(dict(f, number=i, views=views))
        first = pulls[0]
        groups.append(dict(key=key,name=first["label"],difficulty=first["difficulty_label"],level=first.get("keystoneLevel"),dungeon=first["dungeon"],
                           pulls=pulls, recorded=len(raw), sources=len({f["code"] for f in raw}),
                           kills=sum(p["kill"] is True for p in pulls), wipes=sum(p["kill"] is False for p in pulls)))
    return sorted(groups,key=lambda g:-len(g["pulls"]))
