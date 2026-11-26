"""回放中的玩家施法和有效伤害，统一使用战斗起点与日志行边界。"""

import csv
import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from django.db.models import Q

from botend.journal_models import JournalEncounter, JournalState
from botend.models import MythicDungeonEnemy, MythicDungeonSpell, WowSpellSnapshot, WowTalentNodeMetadata
from botend.services import combat_log_localization as localization
from botend.services.combat_log_facts import DAMAGE, pull_catalog
from botend.services.published_wow_icons import published_wow_icon_url
from botend.services.site_image_assets import published_image_url
from botend.services.wow_skill_report_metadata import _tooltip_icon
from botend.templatetags.wow_tags import wow_icon_oss_url
from botend.wow.talents.versioning import TalentVersionResolver

CHUNK_MS = 30000
CAST_WINDOW_MS = 15000
DAMAGE_REVISION = 2
_DAMAGE_KINDS = tuple(sorted(DAMAGE))
_DAMAGE_MARKERS = ",".join("?" for _ in _DAMAGE_KINDS)
_CAST_KINDS = "f.kind IN ('SPELL_CAST_SUCCESS','SPELL_CAST_START','SPELL_CAST_FAILED')"
_PULL = "CASE WHEN f.boss>0 THEN -f.boss ELSE f.wave END"


def _scope(fight):
    axis = "keystone" if fight["kind"] == "钥匙" else "boss"
    return f"f.{axis}=? AND f.line BETWEEN ? AND ?", (fight["id"], fight["start_line"], fight["end_line"])


def boss_groups(db, report, fight):
    """按实际参战单位识别首领，负编号对应首领战，正编号对应大秘境波次。"""
    def resolve():
        scope, params = _scope(fight)
        members = list(db.execute(f"""
            WITH scoped AS (
                SELECT {_PULL} pull,f.src,f.dst
                FROM fact f WHERE {scope}
            ), units AS (
                SELECT pull,src id FROM scoped WHERE pull!=0
                UNION SELECT pull,dst FROM scoped WHERE pull!=0
            )
            SELECT u.pull,a.id,a.guid,a.name,raw.name original FROM units u
            JOIN actor a ON a.id=u.id JOIN main.actor raw ON raw.id=a.id
            WHERE a.family IN ('Creature','Vehicle') ORDER BY u.pull,a.id""", params))
        if not members:
            return {}
        npc_ids = {localization.npc_id(r["guid"]) for r in members} - {0}
        known = set(MythicDungeonEnemy.objects.filter(
            dungeon__data_version__is_active=True, is_active=True, is_boss=True, npc_id__in=npc_ids
        ).values_list("npc_id", flat=True))
        names = defaultdict(set)
        by_encounter = defaultdict(list)
        for fid, eid, name, original in db.execute("""
            SELECT f.id,f.game_id,f.name,r.name FROM fight f JOIN main.fight r ON r.id=f.id
            WHERE f.kind='首领' AND (f.id=? OR f.parent=?)""", (fight["id"],fight["id"])):
            names[-fid].update(n.casefold() for n in (name,original) if n)
            by_encounter[localization.number(eid)].append(-fid)
        if by_encounter:
            release = JournalState.objects.filter(pk="wow-zhCN").values_list("active_release_id",flat=True).first()
            if release:
                for payload in JournalEncounter.objects.filter(
                    instance__release_id=release, payload__dungeon_encounter_id__in=list(by_encounter)
                ).values_list("payload",flat=True):
                    for pull in by_encounter[localization.number(payload["dungeon_encounter_id"])]:
                        names[pull].update(c["name"].casefold() for c in payload.get("creatures",[]) if c.get("name"))
        result = defaultdict(list)
        for row in members:
            pull = row["pull"]
            if localization.npc_id(row["guid"]) in known or any(
                name and name.casefold() in names[pull] for name in (row["name"],row["original"])
            ):
                result[pull].append(row["id"])
        return dict(result)
    return localization.cached(report, f"boss-groups:{fight['id']}", resolve)


def _cast_spell_query(fight, bosses, joins=""):
    scope, params = _scope(fight)
    actors = "s.family='Player'"
    if bosses:
        actors += f""" OR ({_PULL},f.src) IN (
            SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)
        )"""
        params = (*params,json.dumps([(pull,actor) for pull,group in bosses.items() for actor in group]))
    return f"FROM fact f JOIN actor s ON s.id=f.src {joins} WHERE {scope} AND ({actors}) AND {_CAST_KINDS}", params


def cast_icons(db, fight, bosses):
    source, params = _cast_spell_query(fight, bosses)
    ids = {int(s) for s, in db.execute("SELECT DISTINCT f.spell "+source, params)
           if s and len(s) <= 10 and s.isascii() and s.isdecimal() and 0 < int(s) <= 2147483647}
    return spell_icons(ids)


@lru_cache(maxsize=2)
def _file_icon_names(path, revision):
    # A current build can refresh its CSV in place.
    with path.open(encoding='utf-8-sig') as source:
        return {row['FileDataID']: row['IconName'] for row in csv.DictReader(source)
                if re.fullmatch(r'[A-Za-z0-9_-]{1,160}', row['IconName'] or '')}


def spell_icons(spell_ids, source_icons=None):
    """批量查找当前页实际出现的技能，沿用回放的图标资源。"""
    ids = {localization.number(s) for s in spell_ids} - {0}
    result = {sid: source_icons[sid] for sid in ids if source_icons and source_icons.get(sid)}
    if ids == result.keys():
        return result
    files, original_urls = {}, {}
    def add(sid, icon):
        if sid in result or not icon:
            return
        file_icon = re.fullmatch(r'https://assets-ng\.maxroll\.gg/wow/icons/([1-9][0-9]{0,9})\.webp', icon)
        if file_icon:
            # This number identifies an image, never a spell or a basename.
            files.setdefault(sid, file_icon[1])
            original_urls.setdefault(sid, icon)
        elif icon.startswith(('https://','/static/')):
            mapped = published_wow_icon_url(published_image_url(icon))
            if mapped.startswith(('/static/', 'https://oss.wowdaily.cn/')):
                result[sid] = mapped
            else:
                original_urls.setdefault(sid, icon)
        elif re.fullmatch(r'[a-zA-Z0-9_-]+',icon) and not icon.isdecimal():
            result[sid] = wow_icon_oss_url(icon)

    for sid, icon in WowSpellSnapshot.objects.filter(branch="wow",spell_id__in=ids).exclude(icon="").order_by(
            "-updated_at","pk").values_list("spell_id","icon"):
        add(sid, icon)
    for sid, icon in WowTalentNodeMetadata.all_objects.filter(
            reference_id__in=ids-result.keys(), localization_only=True, name_kind="spell",
            talent_version__branch="retail"
    ).exclude(icon="").order_by("-last_updated", "pk").values_list("reference_id","icon"):
        add(sid, icon)
    missing = ids-result.keys()
    for sid, display, icon in WowTalentNodeMetadata.objects.filter(
            Q(display_spell_id__in=missing) | Q(spell_id__in=missing), talent_version__branch="retail"
    ).exclude(icon="").order_by("-talent_version__is_active","-talent_version_id","pk").values_list(
            "spell_id","display_spell_id","icon"):
        for key in (display, sid):
            if key in missing:
                add(key, icon)
    for sid, icon, url in MythicDungeonSpell.objects.filter(
            spell_id__in=ids-result.keys(), is_active=True, data_version__is_active=True
    ).values_list("spell_id", "icon_name", "icon_url"):
        add(sid, icon or url)
    unresolved = files.keys() - result.keys()
    if unresolved:
        version = TalentVersionResolver.get_default()
        if version and version.source_dir:
            try:
                path = Path(version.source_dir) / 'file_data_icon_cache.csv'
                st = path.stat()
                names = _file_icon_names(path, (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size))
            except (OSError, KeyError, UnicodeError, csv.Error):
                names = {}
            for sid in unresolved:
                add(sid, names.get(files[sid]))
    return original_urls | result


def missing_cast_icon(db, fight, sid, bosses):
    source, params = _cast_spell_query(fight, bosses)
    if not db.execute("SELECT 1 "+source+" AND f.spell=? LIMIT 1", (*params,str(sid))).fetchone():
        return ""
    slug = _tooltip_icon("wow", sid)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", slug or ""):
        return ""
    source = f"https://wow.zamimg.com/images/wow/icons/small/{slug}.jpg"
    destination = published_wow_icon_url(published_image_url(source))
    # The report-scoped redirect only serves delivered icons; never send this
    # private lookup through to the third-party image host on a miss.
    return destination if destination != source else ""


def _damage_rows(db, fight, start, end, interval):
    scope, params = _scope(fight)
    # 钥匙里正数为小怪波次，负数为首领交战，0 只计入整场。
    join = ""
    pull = "0"
    if fight["kind"] == "钥匙":
        join = """LEFT JOIN wave w ON w.id=f.wave AND w.keystone=f.keystone
                  AND f.line BETWEEN w.first_line AND w.last_line AND f.ms BETWEEN w.start_ms AND w.end_ms
                  LEFT JOIN fight b ON b.id=f.boss AND b.parent=f.keystone
                  AND f.line BETWEEN b.start_line AND b.end_line AND f.ms>=b.start_ms
                  AND (b.end_ms IS NULL OR f.ms<=b.end_ms)"""
        pull = "COALESCE(-b.id,w.id,0)"
    return db.execute(
        f"""SELECT (f.ms-?)/? tick,f.credited,COALESCE(SUM(f.amount),0),COUNT(*)-COUNT(f.amount),{pull} pull_id
        FROM fact f JOIN actor owner ON owner.id=f.credited {join}
        WHERE {scope} AND f.ms BETWEEN ? AND ? AND owner.family='Player'
        AND f.kind IN ({_DAMAGE_MARKERS})
        GROUP BY tick,f.credited,pull_id ORDER BY tick,f.credited,pull_id""",
        (fight["start_ms"], interval, *params, start, end, *_DAMAGE_KINDS),
    )


def index_damage(db, fights):
    db.execute("""CREATE TABLE replay_damage(
        fight INTEGER,bucket INTEGER,actor INTEGER,amount INTEGER,unknown INTEGER,pull INTEGER,
        PRIMARY KEY(fight,bucket,actor,pull)) WITHOUT ROWID""")
    for fight in fights:
        if fight["start_ms"] is None:
            continue
        end = fight["end_ms"] if fight["end_ms"] is not None else 2**63-1
        rows = _damage_rows(db, fight, fight["start_ms"], end, CHUNK_MS)
        db.executemany("INSERT INTO replay_damage VALUES(?,?,?,?,?,?)", ((fight["id"], *r) for r in rows))


def checkpoints(db, fight, end, revision):
    if revision == DAMAGE_REVISION:
        rows = db.execute("SELECT bucket,actor,amount,unknown,pull FROM replay_damage WHERE fight=? ORDER BY bucket,actor,pull",
                          (fight["id"],))
    else:
        rows = _damage_rows(db, fight, fight["start_ms"], end, CHUNK_MS)
    return [tuple(row) for row in rows]


def pulls(db, fight, end):
    rows = pull_catalog(db,dict(fight,end_ms=end))
    return sorted(({k:p[k] for k in ("id","t","end","label","caption")} for p in rows), key=lambda p:p["t"])


def read_casts(db, fight, start, end, bosses):
    origin = fight["start_ms"]
    joins = "JOIN ability a ON a.id=f.spell LEFT JOIN actor d ON d.id=f.dst"
    source, params = _cast_spell_query(fight, bosses, joins)
    rows = db.execute(
        f"""SELECT f.ms-? t,f.line,f.src actor,f.kind,f.spell spell_id,a.name spell,d.name target
        {source}
        AND f.ms BETWEEN ? AND ?
        ORDER BY f.ms,f.line""",
        (origin, *params, start, end),
    )
    return [dict(r) for r in rows]


def read_chunk(db, fight, start, end, bosses):
    return {
        "casts": read_casts(db, fight, max(fight["start_ms"], start-CAST_WINDOW_MS), end, bosses),
        "damage": [tuple(r) for r in _damage_rows(db, fight, start, end, 1)],
    }
