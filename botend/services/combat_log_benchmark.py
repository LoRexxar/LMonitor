"""从参战事实和站内已采集的 WCL 公开排行中选取对照数据。"""

import re
from collections import Counter
from urllib.parse import quote

from django.db.models import Q

from botend.constants.wow import DUNGEON_CN, RAID_BOSS_CN, SPEC_IDENTITY_MAP
from botend.models import MythicDungeon, SeasonMeta, SpecDungeonRanking, SpecRaidRanking
from botend.services import combat_log_analysis as analysis
from botend.services import combat_log_visuals as visuals
from botend.services import combat_log_duel as duel
from botend.services import combat_log_localization as localization


def participants(sample):
    with analysis.database(sample["report"]) as db:
        return visuals.participants(db, sample["report"], sample)


def performance(sample, actor, metric):
    with analysis.database(sample["report"]) as db:
        return _performance(db, sample, actor, metric)


def _performance(db, sample, actor, metric):
    direction = "target" if metric in ("taken", "deaths") else "source"
    params = {"pets": "owner", direction: actor["guid"]}
    sql, args, value, _, _, _ = analysis.build_selection(sample, params, metric, None)
    rows = db.execute(f"SELECT f.spell,a.name,SUM({value}),COUNT(*),COUNT({value})"
                      + sql + " GROUP BY f.spell,a.name ORDER BY 3 DESC", args)
    skills = [dict(spell=spell, name=name, amount=amount if hits == known else None, hits=hits)
              for spell, name, amount, hits, known in rows]
    total = sum(s["amount"] for s in skills) if all(s["amount"] is not None for s in skills) else None
    start, end = sample["start_ms"], sample["end_ms"]
    seconds = (end - start) / 1000 if start is not None and end is not None and end > start else None
    return dict(name=actor["name"], amount=total, seconds=seconds,
                rate=total / seconds if total is not None and seconds else None, skills=skills)


def _spell_label(spell, translated, local_name, public_names):
    labels = [translated.get(int(spell)) if spell.isdecimal() else None, local_name, public_names.get(spell)]
    return next((name for name in labels if name and not re.fullmatch(r"技能\s*#?\d+", name)),
                f"技能 {spell}" if spell.isdecimal() else "未知机制")


def public_comparison(sample, actor, public, detail, scale):
    mechanisms = []
    with analysis.database(sample["report"]) as db:
        local = {k: _performance(db, sample, actor, k) for k in duel.DETAILS}
        if detail in ("interrupts", "dispels"):
            sql, args, _, _, _, _ = analysis.build_selection(sample, {"pets": "owner", "source": actor["guid"]}, detail, None)
            here = {spell: (name, hits) for spell, name, hits in db.execute(
                "SELECT COALESCE(f.extra_spell,'unknown'),(SELECT name FROM ability WHERE id=f.extra_spell),COUNT(*)"
                + sql + " GROUP BY f.extra_spell", args)}
            there = public["responses"][detail]
            names = localization.spell_names({int(k): public["names"].get(k, "") for k in there if k.isdecimal()})
            for spell in here.keys() | there.keys():
                label, count = here.get(spell, (None, 0))
                mechanisms.append(dict(name=_spell_label(spell, names, label, public["names"]),
                                       spell=spell, delta=duel.contrast(count, there.get(spell, 0))))
            mechanisms.sort(key=lambda row: (-row["delta"]["gap"], row["name"], row["spell"]))
    totals = {k: (local[k]["amount"], sum(public["metrics"][k].values())) for k in duel.DETAILS}
    local_skills = {r["spell"]: r for r in local[detail]["skills"]}
    remote = public["metrics"][detail]
    original = {int(k): public["names"].get(k, "") for k in remote if k.isdecimal()}
    translated = localization.spell_names(original)
    seconds = [local[detail]["seconds"], public["seconds"]]
    factor = 60 if detail in duel.COUNT_METRICS else 1
    def rate(value, duration):
        return value * factor / duration if value is not None and duration else None
    rows = []
    for spell in local_skills.keys() | remote.keys():
        here = local_skills.get(spell, {})
        values = (here.get("amount", 0), remote.get(spell, 0))
        name = _spell_label(spell, translated, here.get("name"), public["names"])
        if scale == "share":
            values = [v * 100 / total if v is not None and total else None
                      for v, total in zip(values, totals[detail])]
        elif scale == "rate":
            values = [rate(v, s) for v, s in zip(values, seconds)]
        rows.append(dict(name=name, spell=spell, delta=duel.contrast(*values)))
    visuals.decorate_spells(rows, "spell")
    rows.sort(key=lambda r: (r["delta"]["gap"] is None, -(r["delta"]["gap"] or 0), r["name"], r["spell"]))
    return dict(public_matrix={k: duel.contrast(*v) for k, v in totals.items()}, public_rows=rows, mechanisms=mechanisms,
                event_seconds=public["seconds"], detail_rate=duel.contrast(*map(rate, totals[detail], seconds)))


def pair_performance(sample, actors, metric, breakdown, measure):
    """双方使用同一组事实与归属规则，先算整体指标，再展开当前切面。"""
    guids = [a["guid"] for a in actors]
    matrix, details = {}, {}
    start, end = sample["start_ms"], sample["end_ms"]
    seconds = (end - start) / 1000 if start is not None and end is not None and end > start else None
    with analysis.database(sample["report"]) as db:
        for key in duel.DETAILS:
            received = key in ("taken", "deaths")
            who = "t.guid" if received else "s.guid"
            sql, args, value, _, _, _ = analysis.build_selection(sample, {"pets": "owner"}, key, None)
            sql += f" AND {who} IN (?,?)"
            totals = [0, 0]
            projection = "0, ''"
            prefix = []
            if key == metric:
                if breakdown == "ability":
                    projection = "f.spell, a.name"
                elif breakdown == "unit":
                    projection = "s.guid, s.name" if received else "t.guid, t.name"
                else:
                    projection = "(f.ms - ?) / 30000, ''"
                    prefix = [start]
            rows = db.execute(f"SELECT {who},{projection},SUM({value}),COUNT(*),COUNT({value})"
                              + sql + " GROUP BY 1,2,3", [*prefix, *args, *guids])
            for guid, ident, label, amount, hits, known in rows:
                i = guids.index(guid)
                verified = amount if known == hits else None
                totals[i] = totals[i] + verified if totals[i] is not None and verified is not None else None
                if key == metric:
                    if breakdown == "time":
                        t = ident * 30
                        label = f"{t // 60}:{t % 60:02d} – {(t + 30) // 60}:{(t + 30) % 60:02d}"
                    row = details.setdefault(ident, dict(id=ident, name=label, values=[0, 0], hits=[0, 0]))
                    row["values"][i], row["hits"][i] = verified, hits
            matrix[key] = dict(values=totals, delta=duel.contrast(*totals))
        if breakdown == "ability":
            visuals.decorate_spells(details.values(), "id", visuals.report_icons(db, sample["report"]))
    selected = matrix[metric]["values"]
    factor = 60 if metric in duel.COUNT_METRICS else 1
    sides = [dict(name=a["name"], **visuals.identity(a["specs"], a.get("class_name")), seconds=seconds, amount=v,
                  rate=v * factor / seconds if seconds and v is not None else None)
             for a, v in zip(actors, selected)]
    label_counts = Counter(row["name"] for row in details.values())
    for row in details.values():
        if label_counts[row["name"]] > 1:
            row["identifier"] = row["id"]
        values = row["values"]
        if measure == "events":
            values = row["hits"]
        elif measure == "average":
            values = [v / n if n and v is not None else None for v, n in zip(values, row["hits"])]
        row["delta"] = duel.contrast(*values)
    if breakdown == "time":
        ordered = sorted(details.values(), key=lambda row: row["id"])
    else:
        ordered = sorted(details.values(), key=lambda row: (
            row["delta"]["gap"] is None, -(row["delta"]["gap"] or 0), row["name"]))
    return dict(results=sides, matrix=matrix, skills=ordered, total_contrast=matrix[metric]["delta"],
                rate_contrast=duel.contrast(*(s["rate"] for s in sides)))


def public_reference(sample, actor):
    dungeon = sample["kind"] == "钥匙"
    if not dungeon and sample["parent"]:
        return None, "公开大秘境榜只记录整把钥匙，当前地下城首领没有独立榜单。"
    level = sample["level"]
    if dungeon and not str(level).isdecimal():
        return None, "这把钥匙的层数未能确认，无法匹配同层公开排名。"
    difficulty = {"15": 4, "16": 5}.get(str(level))
    if not dungeon and not difficulty:
        return None, "公开团本缓存目前覆盖英雄和史诗难度。"
    build = sample["report"].summary["sessions"][sample["session"] - 1]["build"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", build):
        return None, "日志版本不足以确定可比较的赛季。"
    patch = ".".join(build.split(".")[:3])
    seasons = SeasonMeta.objects.filter(Q(game_build=patch) | Q(game_build__startswith=patch + "."))
    names = {sample["name"]}
    for original in sample["report"].summary.get("activity_index", []):
        if original["id"] == sample["id"]:
            names.add(original["name"])
            break
    if dungeon and sample.get("challenge_id"):
        for english, chinese in MythicDungeon.objects.filter(
                map_id=sample["challenge_id"], is_active=True, data_version__is_active=True
        ).values_list("name", "name_zh"):
            names.update((english, chinese))
    translations = DUNGEON_CN if dungeon else RAID_BOSS_CN
    names.update(english for english, chinese in translations.items() if chinese in names)
    matches = []
    for season in seasons:
        encounters = season.mplus_encounters if dungeon else season.raid_encounters
        ids = {e["id"] for e in encounters if e.get("name") in names and e.get("name") and e.get("id")}
        matches.extend((season, eid) for eid in ids)
    if len(matches) != 1:
        return None, "当前版本暂无可唯一匹配此副本或首领的公开排行。"
    season, eid = matches[0]
    if len(actor["specs"]) != 1:
        return None, "日志没有确认该角色在本场的唯一专精，无法匹配同专精标杆。"
    cls, spec = SPEC_IDENTITY_MAP[next(iter(actor["specs"]))]
    filters = dict(season_id=season.pk, class_name=cls, spec_name=spec, dps__gt=0,
                   fight_id__gt=0, report_code__isnull=False)
    if dungeon:
        records = SpecDungeonRanking.objects.filter(**filters, dungeon_id=eid, keystone_level=level,
                                                    score__isnull=False).order_by("-score", "-dps", "pk")
    else:
        records = SpecRaidRanking.objects.filter(**filters, boss_id=eid, difficulty=difficulty
                                                ).order_by("-dps", "pk")
    best = records.exclude(report_code="").first()
    if not best:
        return None, "已采集的公开榜单中暂无同专精、同层数或难度的记录。"
    duration = best.clear_time if dungeon else best.kill_time
    return dict(name=best.character_name, realm=best.realm, spec=spec, dps=best.dps,
                report_code=best.report_code, fight_id=best.fight_id, encounter_id=eid, class_name=cls,
                level_field="keystoneLevel" if dungeon else "difficulty", level=int(level) if dungeon else difficulty,
                score=best.score if dungeon else None, seconds=duration / 1000 if duration else None,
                season=season.season_name, updated=best.last_updated,
                url=f"https://www.warcraftlogs.com/reports/{quote(best.report_code, safe='')}#fight={best.fight_id}"), ""
