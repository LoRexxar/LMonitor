"""按游戏 ID 为私有日志索引提供中文读视图，归档和原始索引保持可核对。"""

import sqlite3
from collections import defaultdict
from contextlib import closing, contextmanager

from django.core.cache import cache
from django.db.models import Q

from botend.journal_models import JournalEncounter, JournalInstance, JournalState
from botend.models import (
    MythicDungeon,
    MythicDungeonAbility,
    MythicDungeonEnemy,
    MythicDungeonSpell,
    WowSpellSnapshot,
    WowTalentNodeMetadata,
)
from botend.services.combat_replay_maps import catalog as floor_catalog
from botend.services.rio_statistics_localization import chinese


def number(value):
    text = str(value or "")
    return int(text) if text.isascii() and text.isdecimal() and len(text) <= 10 else 0


def npc_id(guid):
    parts = str(guid or "").split("-")
    return (
        number(parts[5])
        if len(parts) == 7 and parts[0] in ("Creature", "Vehicle")
        else 0
    )


def _add(names, identity, *labels):
    if identity and identity not in names:
        for label in labels:
            if chinese(label):
                names[identity] = label.strip()
                break


def spell_names(original):
    names = {}
    ids = sorted(original)
    for offset in range(0, len(ids), 400):
        batch = ids[offset : offset + 400]
        references = (
            WowTalentNodeMetadata.all_objects.filter(
                localization_only=True,
                name_kind="spell",
                reference_id__in=batch,
            )
            .exclude(name_zh="")
            .order_by("-last_updated", "pk")
            .values_list("reference_id", "name", "name_zh", "talent_version__branch")
        )
        compatible = defaultdict(set)
        for sid, english, zh, branch in references:
            if branch == "retail":
                _add(names, sid, zh)
            elif (
                chinese(zh)
                and english
                and english.casefold() == original[sid].casefold()
            ):
                compatible[sid].add(zh.strip())
        for sid, published, zh, name in WowSpellSnapshot.objects.filter(
            branch="wow", locale="zhCN", spell_id__in=batch
        ).values_list("spell_id", "display_data__default__name_zh", "name_zh", "name"):
            _add(names, sid, published, zh, name)
        for sid, zh, name in (
            MythicDungeonSpell.objects.filter(
                data_version__is_active=True, is_active=True, spell_id__in=batch
            )
            .exclude(source_branch__in=("wowt", "wowxptr", "wow_beta", "ptr", "beta"))
            .order_by("-updated_at", "pk")
            .values_list("spell_id", "name_zh", "name")
        ):
            _add(names, sid, zh, name)
        talents = (
            WowTalentNodeMetadata.objects.filter(
                Q(display_spell_id__in=batch) | Q(spell_id__in=batch)
            )
            .exclude(name_zh="")
            .order_by("-talent_version__is_active", "-last_updated", "pk")
            .values_list(
                "display_spell_id",
                "spell_id",
                "name_zh",
                "name",
                "talent_version__branch",
            )
        )
        for display, sid, zh, english, branch in talents:
            if branch == "retail" and (display or sid) in original:
                _add(names, display or sid, zh)
            for candidate in {display, sid}:
                if (
                    candidate in original
                    and english
                    and english.casefold() == original[candidate].casefold()
                    and chinese(zh)
                ):
                    compatible[candidate].add(zh.strip())
        for sid, zh, name in (
            MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version__is_active=True,
                is_active=True,
                spell_id__in=set(batch) - names.keys(),
            )
            .order_by("pk")
            .values_list("spell_id", "name_zh", "name")
        ):
            _add(names, sid, zh, name)
        # 共享名称库也可能收录在先行版本。仅接受同技能 ID、相同原名且译名一致的记录。
        for sid, labels in compatible.items():
            if len(labels) == 1:
                _add(names, sid, next(iter(labels)))
    return names


def _fight_names(rows):
    names = {}
    maps = {number(r["game_id"]) for r in rows if r["kind"] in ("钥匙", "地下城交战")}
    bosses = {number(r["game_id"]) for r in rows if r["kind"] == "首领"}
    if not (maps or bosses):
        return names
    release = (
        JournalState.objects.filter(pk="wow-zhCN")
        .values_list("active_release_id", flat=True)
        .first()
    )
    instances, encounters, dungeons, by_slug = {}, {}, {}, {}
    if release:
        for mid, name in JournalInstance.objects.filter(
            release_id=release, payload__map_id__in=maps
        ).values_list("payload__map_id", "name"):
            _add(instances, number(mid), name)
        for eid, name in JournalEncounter.objects.filter(
            instance__release_id=release, payload__dungeon_encounter_id__in=bosses
        ).values_list("payload__dungeon_encounter_id", "name"):
            _add(encounters, number(eid), name)
    # 旧索引只保存实例地图 ID；楼层校准资料提供已确认的实例到 MDT 副本键映射。
    slugs = defaultdict(set)
    if maps:
        for floor in floor_catalog()["floors"].values():
            slug = (floor.get("mdt") or {}).get("slug")
            for mid in floor["instances"]:
                if mid in maps and slug:
                    slugs[mid].add(slug)
        challenges = {
            number(r.get("challenge_id")) for r in rows if r["kind"] == "钥匙"
        }
        keys = {slug for values in slugs.values() for slug in values}
        for mid, key, zh, name in (
            MythicDungeon.objects.filter(
                Q(map_id__in=challenges) | Q(key__in=keys),
                data_version__is_active=True,
                is_active=True,
            )
            .order_by("-data_version_id", "pk")
            .values_list("map_id", "key", "name_zh", "name")
        ):
            _add(dungeons, mid, zh, name)
            _add(by_slug, key, zh, name)
    for row in rows:
        mid = number(row["game_id"])
        if row["kind"] in ("钥匙", "地下城交战"):
            keys = slugs.get(mid, ())
            slug_name = by_slug.get(next(iter(keys))) if len(keys) == 1 else None
            _add(
                names,
                row["id"],
                instances.get(mid),
                dungeons.get(number(row.get("challenge_id"))),
                slug_name,
            )
        else:
            _add(names, row["id"], encounters.get(mid))
    return names


def cached(report, kind, load):
    key = f"combat-zh-v1:{report.pk}:{report.published}:{kind}"
    result = cache.get(key)
    if result is None:
        result = load()
        cache.set(key, result, 60)
    return result


def npc_names(ids):
    translated = {}
    ids = sorted(set(ids) - {0})
    for offset in range(0, len(ids), 400):
        rows = (
            MythicDungeonEnemy.objects.filter(
                dungeon__data_version__is_active=True,
                is_active=True,
                npc_id__in=ids[offset : offset + 400],
            )
            .order_by("-dungeon__data_version_id", "pk")
            .values_list("npc_id", "name_zh", "name")
        )
        for nid, zh, name in rows:
            _add(translated, nid, zh, name)
    return translated


def fight_labels(report, rows):
    return cached(report, "fights", lambda: _fight_names(rows))


@contextmanager
def database(report, path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row

        def dimensions():
            tables = {
                name
                for (name,) in db.execute(
                    "SELECT name FROM main.sqlite_master WHERE type='table'"
                )
            }
            source = (
                "SELECT id,name FROM main.ability"
                if "ability" in tables
                else "SELECT DISTINCT spell_id,spell FROM main.interrupt"
            )
            spells = {number(s): name for s, name in db.execute(source) if number(s)}
            npcs = (
                npc_names(
                    npc_id(guid)
                    for (guid,) in db.execute(
                        "SELECT guid FROM main.actor WHERE family IN ('Creature','Vehicle')"
                    )
                )
                if "actor" in tables
                else {}
            )
            return (
                {str(k): v for k, v in spell_names(spells).items()},
                npcs,
                "ability" in tables,
            )

        spells, npcs, has_abilities = cached(report, "entities", dimensions)
        db.create_function(
            "cn_spell",
            2,
            lambda sid, name: spells.get(str(sid), name),
            deterministic=True,
        )
        db.create_function(
            "cn_actor",
            2,
            lambda guid, name: npcs.get(npc_id(guid), name),
            deterministic=True,
        )
        if spells and has_abilities:
            db.execute(
                "CREATE TEMP VIEW ability AS SELECT id,cn_spell(id,name) name FROM main.ability"
            )
        if npcs:
            db.execute(
                "CREATE TEMP VIEW actor AS SELECT id,guid,cn_actor(guid,name) name,family FROM main.actor"
            )
        fights = cached(
            report,
            "fights",
            lambda: _fight_names(
                [dict(r) for r in db.execute("SELECT * FROM main.fight")]
            ),
        )
        if fights:
            db.create_function(
                "cn_fight",
                2,
                lambda fid, name: fights.get(fid, name),
                deterministic=True,
            )
            fields = [
                "cn_fight(id,name) AS name" if c["name"] == "name" else c["name"]
                for c in db.execute("PRAGMA main.table_info(fight)")
            ]
            db.execute(
                "CREATE TEMP VIEW fight AS SELECT "
                + ",".join(fields)
                + " FROM main.fight"
            )
        yield db
