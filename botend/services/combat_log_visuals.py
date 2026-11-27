"""战斗分析共用的参战身份、技能图标与天赋快照读视图。"""

import json
import re

from django.db.models import Case, IntegerField, Value, When

from botend.constants.wow import CLASS_CN, specialization_catalog
from botend.models import WowTalentNodeMetadata
from botend.services import combat_log_localization as localization
from botend.services.content_image_display import display_image_url
from botend.services.combat_replay_activity import published_spell_icon, spell_icons
from botend.templatetags.wow_tags import wow_icon_oss_url

SPECS = {s["spec_id"]: s for s in specialization_catalog()}


def identity(spec_ids, class_name=""):
    specs = [SPECS[i] for i in sorted(spec_ids) if i in SPECS]
    if len(specs) == 1:
        return dict(icon=display_image_url(specs[0]["icon"]), icon_label=specs[0]["label"])
    if specs and len({s["class_name"] for s in specs}) == 1:
        return dict(icon=wow_icon_oss_url("classicon_" + specs[0]["class_name"].lower()),
                    icon_label=specs[0]["class_label"] + " · " + " / ".join(s["spec_label"] for s in specs))
    if class_name in CLASS_CN:
        return dict(icon=wow_icon_oss_url("classicon_" + class_name.lower()), icon_label=CLASS_CN[class_name])
    return {}


def _report_icon(icon):
    if not isinstance(icon, str):
        return ''
    if icon.startswith(('https://', '/static/')):
        mapped = published_spell_icon(icon)
        return mapped if mapped.startswith(('https://oss.wowdaily.cn/', '/static/')) else ''
    match = re.fullmatch(r'([A-Za-z0-9_-]{1,160})(?:\.jpg)?', icon, re.I)
    if match and not match[1].isdecimal() and match[1].lower() != 'inv_misc_questionmark':
        return published_spell_icon(match[1])
    return ''


def report_icons(db, report):
    if hasattr(report, "wcl_source") and report.summary.get("talent_revision"):
        return localization.cached(report, "wcl-icon-slugs", lambda: {
            sid: url for sid, icon in db.execute("SELECT spell,icon FROM wcl_icon") if (url := _report_icon(icon))
        })
    return {}


def participants(db, report, sample):
    def collect():
        axis = "keystone" if sample["kind"] == "钥匙" else "boss"
        rows = db.execute(f"""
            SELECT a.guid,a.name,p.spec FROM (
                SELECT src AS actor,src_spec AS spec FROM fact WHERE {axis}=?
                UNION SELECT dst,dst_spec FROM fact WHERE {axis}=?
            ) p JOIN actor a ON a.id=p.actor WHERE a.family='Player' ORDER BY a.name,a.guid
        """, (sample["id"], sample["id"]))
        actors = {}
        for guid, name, spec in rows:
            actor = actors.setdefault(guid, dict(guid=guid, name=name, specs=set()))
            if spec in SPECS:
                actor["specs"].add(spec)
        if hasattr(report, "wcl_source") and report.summary.get("talent_revision"):
            for guid, cls in db.execute("SELECT guid,class_name FROM wcl_class"):
                if guid in actors and cls in CLASS_CN:
                    actors[guid]["class_name"] = cls
        return actors
    return localization.cached(report, f"roster:{sample['id']}", collect)


def decorate_spells(rows, field, source_icons=None):
    icons = spell_icons([r[field] for r in rows], source_icons)
    for row in rows:
        row["icon_id"] = localization.number(row[field])
        row["icon"] = icons.get(row["icon_id"])


def decorate(ctx):
    groups = [(ctx["group"], ctx["rows"]), (ctx["cross"], ctx["headers"]),
              (ctx["group"], ctx.get("curves", []))]
    ids = [r["ident"] for axis, rows in groups if axis == "ability" for r in rows]
    events = ctx["events"]
    coverage = ctx.get("coverage", [])
    icons = spell_icons(ids + [e["spell_id"] for e in events] + [r["spell"] for r in coverage], ctx["report_icons"])
    melee_icon = published_spell_icon("ability_meleedamage") if "SWING_DAMAGE" in ids or any(
        e["spell_id"] == "SWING_DAMAGE" for e in events) else ""
    for row in coverage:
        row["icon"] = icons.get(localization.number(row["spell"]))
    for axis, rows in groups:
        for r in rows:
            key = r["ident"]
            if axis == "ability":
                r["icon_id"] = localization.number(key)
                r["icon"] = melee_icon if key == "SWING_DAMAGE" else icons.get(r["icon_id"], "")
            elif axis == "spec":
                r.update(identity([localization.number(key)]))
            elif axis in ("source", "target"):
                r.update(identity(ctx["actor_specs"].get(key, ()), ctx["actor_classes"].get(key)))
    for event in events:
        event["icon"] = melee_icon if event["spell_id"] == "SWING_DAMAGE" else icons.get(localization.number(event["spell_id"]), "")
        for side in ("source", "target"):
            event[side + "_identity"] = identity([event[side + "_spec"]], ctx["actor_classes"].get(event[side + "_guid"]))


def talents(db, report, fight, actor):
    if not report.summary.get("talent_revision"):
        return None
    # 取本次参战前最近的配置，也保留长钥匙中途记录的换天赋。
    first = db.execute("""SELECT MIN(line) FROM (
        SELECT MIN(line) line FROM fact WHERE src=? AND line BETWEEN ? AND ?
        UNION ALL SELECT MIN(line) FROM fact WHERE dst=? AND line BETWEEN ? AND ?)""",
        (actor["id"], fight["start_line"], fight["end_line"])*2).fetchone()[0]
    args = (fight["session"], actor["guid"])
    opening = db.execute("""SELECT line,spec,entries FROM combatant_build
        WHERE session=? AND guid=? AND line<=? ORDER BY line DESC LIMIT 1""", (*args, first)).fetchone()
    snapshots = ([opening] if opening else []) + list(db.execute("""SELECT line,spec,entries FROM combatant_build
        WHERE session=? AND guid=? AND line>? AND line<=? ORDER BY line""", (*args, first, fight["end_line"])))
    builds = []
    previous = None
    for line, spec, payload in snapshots:
        entries = json.loads(payload)
        if (spec, entries) != previous:
            builds.append(dict(line=line, spec=spec, entries=entries, **identity([spec])))
        previous = spec, entries
    ids = {item[0] for b in builds for item in b["entries"]}
    metadata = {}
    classes = {SPECS[b["spec"]]["class_name"] for b in builds if b["spec"] in SPECS}
    patch = report.summary["sessions"][fight["session"]-1]["build"]
    nodes = WowTalentNodeMetadata.objects.filter(node_id__in=ids, class_name__in=classes,
                                                  name_kind="talent", talent_version__branch="retail")
    # 同一条目优先使用与日志构建相同的资料，其次使用生效正式服资料。
    priority = Case(When(talent_version__current_build=patch, then=Value(0)),
                    When(talent_version__current_build__startswith=patch+".", then=Value(1)),
                    default=Value(2), output_field=IntegerField())
    for row in nodes.order_by(priority, "-talent_version__is_active", "-talent_version_id", "pk").values(
            "node_id", "talent_id", "class_name", "spec_name", "name_zh", "name", "icon", "display_spell_id", "spell_id"):
        metadata.setdefault((row["node_id"], row["class_name"], row["spec_name"]), row)
    fallback_ids = [row["display_spell_id"] or row["spell_id"] for row in metadata.values() if not row["icon"]]
    source_spells = {item[3] for b in builds for item in b["entries"] if item[3]}
    missing = spell_icons(fallback_ids + list(source_spells), report_icons(db, report))
    names = localization.spell_names({sid: "" for sid in source_spells})
    for build in builds:
        spec = SPECS.get(build["spec"], {})
        items = []
        for entry, rank, node, spell in build.pop("entries"):
            row = metadata.get((entry, spec.get("class_name"), spec.get("spec_name")), {})
            if node and row.get("talent_id") and node != row["talent_id"]:
                row = {}
            icon = row.get("icon") or ""
            if icon and not icon.startswith(("https://", "/static/")):
                icon = wow_icon_oss_url(icon)
            items.append(dict(entry=entry, rank=rank, icon_id=spell or row.get("display_spell_id") or row.get("spell_id"),
                              name=row.get("name_zh") or names.get(spell) or row.get("name") or f"天赋 #{entry}",
                              icon=display_image_url(icon) or missing.get(spell or row.get("display_spell_id") or row.get("spell_id"))))
        build["items"] = items
    return builds
