# -*- coding: utf-8 -*-
"""Repair existing talent metadata names/descriptions from local DB2 CSV dumps.

This command is intentionally separate from backfill_db2_talent_nodes:
- backfill_db2_talent_nodes creates missing nodes.
- this command updates already-existing rows whose names/descriptions came from
  stale player API snapshots or were left blank.
"""

import csv
import os
from dataclasses import dataclass

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata
from botend.wow.spell_text import resolve_spell_text


@dataclass
class TraitDefData:
    spell_id: int = 0
    visible_spell_id: int = 0
    override_spell_id: int = 0
    override_name: str = ""
    override_desc: str = ""


class Command(BaseCommand):
    help = "从本地 DB2 CSV 修复已存在天赋节点的名称和描述"

    def add_arguments(self, parser):
        parser.add_argument("--dump-dir", default=".cache/wago_db2_dumps/latest", help="DB2 dump 目录")
        parser.add_argument("--class-name", default="", help="仅处理指定职业")
        parser.add_argument("--dry-run", action="store_true", help="只输出统计，不写入")
        parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条（调试用）")
        parser.add_argument(
            "--skip-snapshot",
            action="store_true",
            help="跳过 WowSpellSnapshot upsert；用于仅重新清洗/回写天赋描述，避免远程 MySQL 逐条查询过慢",
        )

    def handle(self, *args, **options):
        dump_dir = options["dump_dir"]
        class_name = options["class_name"]
        dry_run = options["dry_run"]
        limit = options["limit"]
        skip_snapshot = options["skip_snapshot"]

        if not os.path.isdir(dump_dir):
            self.stderr.write(self.style.ERROR(f"DB2 dump 目录不存在: {dump_dir}"))
            return

        entries = self._load_entries(dump_dir)
        defs_zh = self._load_defs(os.path.join(dump_dir, "TraitDefinition_zhCN.csv"))
        defs_en = self._load_defs(os.path.join(dump_dir, "TraitDefinition_enUS.csv"))
        if not defs_zh:
            defs_zh = self._load_defs(os.path.join(dump_dir, "TraitDefinition.csv"))
        if not defs_en:
            defs_en = defs_zh
        spell_names_zh = self._load_spell_names(os.path.join(dump_dir, "SpellName_zhCN.csv"))
        spell_names_en = self._load_spell_names(os.path.join(dump_dir, "SpellName_enUS.csv"))
        spell_descs_zh = self._load_spell_descs(os.path.join(dump_dir, "Spell_zhCN.csv"))

        self.stdout.write(
            f"加载完成: entries={len(entries)}, defs_zh={len(defs_zh)}, "
            f"spell_names_zh={len(spell_names_zh)}, spell_descs={len(spell_descs_zh)}"
        )

        qs = WowTalentNodeMetadata.objects.all().order_by("id")
        if class_name:
            qs = qs.filter(class_name=class_name)
        if limit:
            qs = qs[:limit]

        to_update = []
        scanned = 0
        stats = {
            "name": 0,
            "name_zh": 0,
            "description": 0,
            "description_zh": 0,
            "snapshot": 0,
        }
        samples = []
        now = timezone.now()

        for obj in qs.iterator(chunk_size=500):
            scanned += 1
            candidate = self._candidate_for(
                obj,
                entries,
                defs_zh,
                defs_en,
                spell_names_zh,
                spell_names_en,
                spell_descs_zh,
            )
            changed_fields = []

            cand_name = candidate["name"][:255]
            cand_name_zh = candidate["name_zh"][:255]
            cand_desc_raw = candidate["description_raw"]
            desc_spell_id = candidate["description_spell_id"] or candidate["primary_spell_id"]
            cand_desc_zh = resolve_spell_text(cand_desc_raw, desc_spell_id) if cand_desc_raw else ""
            cand_desc_en = cand_desc_zh  # 当前本地只有 zhCN Spell CSV；先保证页面中文描述正确。

            if cand_name and self._should_replace_name(obj.name, cand_name):
                obj.name = cand_name
                changed_fields.append("name")
                stats["name"] += 1
            if cand_name_zh and self._should_replace_name(obj.name_zh, cand_name_zh):
                obj.name_zh = cand_name_zh
                changed_fields.append("name_zh")
                stats["name_zh"] += 1

            if cand_desc_en and self._should_replace_desc(obj.description, cand_desc_en):
                obj.description = cand_desc_en
                changed_fields.append("description")
                stats["description"] += 1
            if cand_desc_zh and self._should_replace_desc(obj.description_zh, cand_desc_zh):
                obj.description_zh = cand_desc_zh
                changed_fields.append("description_zh")
                stats["description_zh"] += 1

            if changed_fields:
                obj.last_updated = now
                changed_fields.append("last_updated")
                to_update.append(obj)
                if len(samples) < 30:
                    samples.append(
                        f"#{obj.id} {obj.class_name}/{obj.spec_name}/{obj.tree_type} "
                        f"node={obj.node_id} spell={obj.spell_id} fields={changed_fields} "
                        f"name='{obj.name_zh or obj.name}' desc='{(obj.description_zh or '')[:80]}'"
                    )

            if not skip_snapshot and candidate["primary_spell_id"] and (cand_name or cand_name_zh or cand_desc_raw):
                self._upsert_snapshot(candidate["primary_spell_id"], cand_name, cand_name_zh, cand_desc_raw, dry_run, stats)

        self.stdout.write(f"扫描 {scanned} 条，待更新 {len(to_update)} 条")
        self.stdout.write(f"字段更新统计: {stats}")
        for s in samples:
            self.stdout.write(f"  {s}")

        if dry_run:
            self.stdout.write(self.style.WARNING("dry-run：未写入数据库"))
            return

        if to_update:
            WowTalentNodeMetadata.objects.bulk_update(
                to_update,
                ["name", "name_zh", "description", "description_zh", "last_updated"],
                batch_size=500,
            )
        self.stdout.write(self.style.SUCCESS(f"完成: 更新 {len(to_update)} 条天赋元数据"))

    @staticmethod
    def _to_int(value):
        try:
            return int(str(value).strip() or "0")
        except Exception:
            return 0

    def _candidate_for(self, obj, entries, defs_zh, defs_en, spell_names_zh, spell_names_en, spell_descs_zh):
        entry_id = self._to_int(obj.node_id)
        def_id = entries.get(entry_id, 0)
        dz = defs_zh.get(def_id, TraitDefData())
        de = defs_en.get(def_id, TraitDefData())

        spell_ids = []
        for sid in (
            dz.visible_spell_id,
            de.visible_spell_id,
            dz.spell_id,
            de.spell_id,
            dz.override_spell_id,
            de.override_spell_id,
            self._to_int(obj.display_spell_id),
            self._to_int(obj.spell_id),
            entry_id,
        ):
            if sid and sid not in spell_ids:
                spell_ids.append(sid)

        name_zh = dz.override_name or ""
        name = de.override_name or ""
        for sid in spell_ids:
            if not name_zh and spell_names_zh.get(sid):
                name_zh = spell_names_zh[sid]
            if not name and spell_names_en.get(sid):
                name = spell_names_en[sid]

        desc = dz.override_desc or ""
        desc_sid = 0
        if not desc:
            for sid in spell_ids:
                if spell_descs_zh.get(sid):
                    desc = spell_descs_zh[sid]
                    desc_sid = sid
                    break

        return {
            "name": name,
            "name_zh": name_zh,
            "description_raw": desc,
            "description_spell_id": desc_sid,
            "primary_spell_id": spell_ids[0] if spell_ids else 0,
        }

    @staticmethod
    def _should_replace_name(current, candidate):
        cur = (current or "").strip()
        cand = (candidate or "").strip()
        if not cand:
            return False
        if not cur:
            return True
        if cur in {"未命名天赋", "Unknown"}:
            return True
        if cur.startswith("技能ID ") or cur.startswith("ID"):
            return True
        # 旧数据经常来自错误 spell_id，显示成 NPC/副本技能名；DB2 candidate 更可信。
        return cur != cand

    @staticmethod
    def _should_replace_desc(current, candidate):
        cur = (current or "").strip()
        cand = (candidate or "").strip()
        if not cand:
            return False
        if not cur:
            return True
        bad_tokens = ("$", "@spell", "<", "|C", "|c", "|R", "|r")
        if any(token in cur for token in bad_tokens):
            return True
        return cur != cand

    @staticmethod
    def _load_entries(dump_dir):
        out = {}
        path = os.path.join(dump_dir, "TraitNodeEntry.csv")
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    out[int(row["ID"])] = int(row.get("TraitDefinitionID") or 0)
                except (TypeError, ValueError):
                    continue
        return out

    def _load_defs(self, path):
        out = {}
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                did = self._to_int(row.get("ID"))
                if not did:
                    continue
                out[did] = TraitDefData(
                    spell_id=self._to_int(row.get("SpellID")),
                    visible_spell_id=self._to_int(row.get("VisibleSpellID")),
                    override_spell_id=self._to_int(row.get("OverridesSpellID")),
                    override_name=(row.get("OverrideName_lang") or "").strip(),
                    override_desc=(row.get("OverrideDescription_lang") or "").strip(),
                )
        return out

    def _load_spell_names(self, path):
        out = {}
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid = self._to_int(row.get("ID"))
                name = (row.get("Name_lang") or "").strip()
                if sid and name:
                    out[sid] = name
        return out

    def _load_spell_descs(self, path):
        out = {}
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid = self._to_int(row.get("ID"))
                desc = (row.get("Description_lang") or row.get("AuraDescription_lang") or "").strip()
                if sid and desc:
                    out[sid] = desc
        return out

    @staticmethod
    def _upsert_snapshot(spell_id, name, name_zh, desc, dry_run, stats):
        if dry_run:
            return
        obj, created = WowSpellSnapshot.objects.get_or_create(
            branch="wow",
            locale="zhCN",
            spell_id=spell_id,
            defaults={"name": name or "", "name_zh": name_zh or "", "description": desc or ""},
        )
        changed = False
        if name and not obj.name:
            obj.name = name
            changed = True
        if name_zh and not obj.name_zh:
            obj.name_zh = name_zh
            changed = True
        if desc and not obj.description:
            obj.description = desc
            changed = True
        if changed:
            obj.save(update_fields=["name", "name_zh", "description", "updated_at"])
            stats["snapshot"] += 1
        elif created:
            stats["snapshot"] += 1
