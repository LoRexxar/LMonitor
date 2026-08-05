# -*- coding: utf-8 -*-
"""Parse Blizzard placeholders in talent descriptions.

Usage:
  python manage.py parse_talent_descriptions --dry-run --limit 20
  python manage.py parse_talent_descriptions --write
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from botend.constants.wow import SPEC_ACTIVE_AURA_IDS, SPEC_CONDITION_INDEX
from botend.models import WowTalentNodeMetadata
from botend.wow.spell_text import SpellTextResolver



class Command(BaseCommand):
    help = "解析天赋描述中的 Blizzard 占位符，可 dry-run 或写回 WowTalentNodeMetadata"

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0, help='最多处理多少条（0=不限制）')
        parser.add_argument('--write', action='store_true', help='写回数据库；默认只预览')
        parser.add_argument('--dry-run', action='store_true', help='只预览，不写入（默认行为）')
        parser.add_argument('--locale', default='zhCN')
        parser.add_argument('--batch-size', type=int, default=500)

    def handle(self, *args, **options):
        limit = int(options.get('limit') or 0)
        write = bool(options.get('write'))
        locale = options.get('locale') or 'zhCN'
        batch_size = int(options.get('batch_size') or 500)
        qs = WowTalentNodeMetadata.objects.filter(
            Q(description__contains='$') | Q(description_zh__contains='$')
        ).select_related('talent_version').order_by('id')
        if limit > 0:
            qs = qs[:limit]

        rows = list(qs)
        version_ids = {row.talent_version_id for row in rows}
        known_by_spec = {}
        for version_id, class_name, spec_name, spell_id, display_spell_id in (
            WowTalentNodeMetadata.objects.filter(talent_version_id__in=version_ids)
            .values_list('talent_version_id', 'class_name', 'spec_name', 'spell_id', 'display_spell_id')
        ):
            key = (version_id, class_name, spec_name)
            known_by_spec.setdefault(key, set()).update(filter(None, (spell_id, display_spell_id)))
        resolver_cache = {}

        total = changed = 0
        to_update = []
        examples = []
        for row in rows:
            total += 1
            spell_id = row.display_spell_id or row.spell_id
            dump_dir = row.talent_version.source_dir or None
            branch = row.talent_version.branch or 'wow'
            snapshot_build = row.talent_version.current_build or ''
            resolver_key_en = (dump_dir, branch, snapshot_build, 'enUS')
            resolver_key_zh = (dump_dir, branch, snapshot_build, locale)
            resolver_en = resolver_cache.setdefault(
                resolver_key_en,
                SpellTextResolver(
                    locale='enUS', branch=branch,
                    snapshot_build=snapshot_build, dump_dir=dump_dir,
                ),
            )
            resolver_zh = resolver_cache.setdefault(
                resolver_key_zh,
                SpellTextResolver(
                    locale=locale, branch=branch,
                    snapshot_build=snapshot_build, dump_dir=dump_dir,
                ),
            )
            context_key = (row.talent_version_id, row.class_name, row.spec_name)
            context = {
                'spec_index': SPEC_CONDITION_INDEX.get((row.class_name, row.spec_name)),
                'known_spell_ids': known_by_spec.get(context_key, set()),
                'active_aura_ids': SPEC_ACTIVE_AURA_IDS.get((row.class_name, row.spec_name), set()),
            }
            new_desc = resolver_en.resolve(row.description or '', spell_id, **context) if row.description else ''
            new_zh = resolver_zh.resolve(row.description_zh or '', spell_id, **context) if row.description_zh else ''

            row_changed = False
            if new_desc and new_desc != (row.description or ''):
                row.description = new_desc
                row_changed = True
            if new_zh and new_zh != (row.description_zh or ''):
                row.description_zh = new_zh
                row_changed = True

            if row_changed:
                changed += 1
                row.last_updated = timezone.now()
                to_update.append(row)
                if len(examples) < 12:
                    examples.append((row.id, spell_id, row.name_zh or row.name, new_zh or new_desc))

            if write and len(to_update) >= batch_size:
                WowTalentNodeMetadata.objects.bulk_update(
                    to_update, ['description', 'description_zh', 'last_updated'], batch_size=batch_size
                )
                to_update.clear()

        if write and to_update:
            WowTalentNodeMetadata.objects.bulk_update(
                to_update, ['description', 'description_zh', 'last_updated'], batch_size=batch_size
            )

        self.stdout.write(f"扫描 {total} 条含占位符描述，{'写回' if write else '可更新'} {changed} 条")
        for db_id, spell_id, name, text in examples:
            self.stdout.write(f"  #{db_id} spell={spell_id} {name}: {text[:220]}")
        if not write:
            self.stdout.write('未写入数据库；确认后使用 --write 执行批量写回。')
