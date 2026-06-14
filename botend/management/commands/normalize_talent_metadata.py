# -*- coding: utf-8 -*-

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.models import WowTalentNodeMetadata


class Command(BaseCommand):
    help = '合并 WoW 天赋元数据中同节点的跨 tree_type 重复行'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--spec-name', default='', help='仅处理指定专精')

    def handle(self, *args, **options):
        class_name = (options.get('class_name') or '').strip()
        spec_name = (options.get('spec_name') or '').strip()

        queryset = WowTalentNodeMetadata.objects.exclude(spell_id__isnull=True)
        if class_name:
            queryset = queryset.filter(class_name=class_name)
        if spec_name:
            queryset = queryset.filter(spec_name=spec_name)

        grouped = defaultdict(list)
        for row in queryset.order_by('class_name', 'spec_name', 'node_id', 'spell_id', 'id'):
            identity = row.node_id or row.talent_id or row.spell_id
            key = (row.class_name, row.spec_name, identity)
            grouped[key].append(row)

        merged_groups = 0
        deleted_rows = 0
        for _, rows in grouped.items():
            if len(rows) <= 1:
                continue
            merged_groups += 1
            keeper = self._pick_keeper(rows)
            merged_values = self._merge_rows(rows, keeper)
            changed = False
            for field, value in merged_values.items():
                if field == 'parents_json':
                    if list(keeper.parents_json or []) != list(value or []):
                        keeper.parents_json = list(value or [])
                        changed = True
                    continue
                if getattr(keeper, field) != value:
                    setattr(keeper, field, value)
                    changed = True
            if changed:
                keeper.last_updated = timezone.now()
                keeper.save(update_fields=[
                    'tree_type', 'display_spell_id', 'talent_id', 'name', 'name_zh', 'icon',
                    'row', 'column', 'max_points', 'parents_json', 'source', 'last_updated',
                ])

            for row in rows:
                if row.id == keeper.id:
                    continue
                row.delete()
                deleted_rows += 1

        self.stdout.write(self.style.SUCCESS(
            f'已合并 {merged_groups} 组重复元数据，删除 {deleted_rows} 条重复行'
        ))

    def _pick_keeper(self, rows):
        target_type = self._pick_tree_type(rows)

        def sort_key(row):
            return (
                1 if row.node_id and row.spell_id == row.node_id else 0,
                1 if row.tree_type == target_type else 0,
                self._score_row(row),
                -row.id,
            )

        return max(rows, key=sort_key)

    @staticmethod
    def _pick_tree_type(rows):
        tree_types = {row.tree_type for row in rows}
        if 'hero' in tree_types:
            return 'hero'
        if 'class' in tree_types:
            return 'class'
        return 'spec'

    @staticmethod
    def _score_row(row):
        score = 0
        if row.display_spell_id:
            score += 2
        if row.name:
            score += 2
        if row.icon:
            score += 2
        if row.row is not None and row.column is not None:
            score += 3
        if row.parents_json:
            score += 3
        if row.tree_type == 'hero':
            score += 1
        if row.tree_type == 'class':
            score += 1
        return score

    def _merge_rows(self, rows, keeper):
        parents = sorted(
            {
                int(parent_id)
                for row in rows
                for parent_id in (row.parents_json or [])
                if str(parent_id).strip().isdigit()
            }
        )
        merged = {
            'tree_type': self._pick_tree_type(rows),
            'display_spell_id': keeper.display_spell_id,
            'talent_id': keeper.talent_id,
            'name': keeper.name,
            'name_zh': keeper.name_zh,
            'icon': keeper.icon,
            'row': keeper.row,
            'column': keeper.column,
            'max_points': keeper.max_points,
            'parents_json': parents,
            'source': keeper.source,
        }

        for row in rows:
            candidate_display_spell_id = row.display_spell_id or (
                row.spell_id if row.node_id and row.spell_id and row.spell_id != row.node_id else None
            )
            if not merged['display_spell_id'] and candidate_display_spell_id:
                merged['display_spell_id'] = candidate_display_spell_id
            if not merged['talent_id'] and row.talent_id:
                merged['talent_id'] = row.talent_id
            if not merged['name'] and row.name:
                merged['name'] = row.name
            if not merged['name_zh'] and row.name_zh:
                merged['name_zh'] = row.name_zh
            if not merged['icon'] and row.icon:
                merged['icon'] = row.icon
            if merged['row'] is None and row.row is not None:
                merged['row'] = row.row
            if merged['column'] is None and row.column is not None:
                merged['column'] = row.column
            if (not merged['max_points'] or merged['max_points'] == 1) and row.max_points:
                merged['max_points'] = row.max_points
            if (not merged['source'] or merged['source'] == 'derived') and row.source:
                merged['source'] = row.source
        return merged
