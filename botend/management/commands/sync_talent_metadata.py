# -*- coding: utf-8 -*-

from collections import OrderedDict

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import (
    PlayerSpecTopPlayer,
    SpecDungeonRanking,
    SpecRaidRanking,
    WowSpellSnapshot,
    WowTalentNodeMetadata,
)
from botend.wow.talents.parser import normalize_talent_payload


class Command(BaseCommand):
    help = '从现有 WoW 数据中同步天赋元数据缓存'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', default='', help='仅同步指定职业')
        parser.add_argument('--spec-name', default='', help='仅同步指定专精')
        parser.add_argument('--limit', type=int, default=0, help='每个数据源限制记录数，0 表示不限制')

    def handle(self, *args, **options):
        class_name = options['class_name']
        spec_name = options['spec_name']
        limit = options['limit']

        total = 0
        total += self._sync_queryset(PlayerSpecTopPlayer.objects.all(), 'top_player', class_name, spec_name, limit)
        total += self._sync_queryset(SpecDungeonRanking.objects.all(), 'dungeon_ranking', class_name, spec_name, limit)
        total += self._sync_queryset(SpecRaidRanking.objects.all(), 'raid_ranking', class_name, spec_name, limit)
        self._reclassify_tree_types(class_name)

        self.stdout.write(self.style.SUCCESS(f'已同步/更新 {total} 条天赋元数据'))

    def _sync_queryset(self, queryset, source, class_name, spec_name, limit):
        if class_name:
            queryset = queryset.filter(class_name=class_name)
        if spec_name:
            queryset = queryset.filter(spec_name=spec_name)
        if limit:
            queryset = queryset[:limit]

        merged_nodes = OrderedDict()
        all_spell_ids = set()
        raw_entries = []
        for row in queryset.iterator() if hasattr(queryset, 'iterator') else queryset:
            payload = normalize_talent_payload(
                getattr(row, 'talents_json', []) or [],
                class_name=row.class_name,
                spec_name=row.spec_name,
            )
            for node in payload['nodes']:
                if node.get('tree_type') == 'build_code':
                    continue
                spell_id = node.get('spell_id')
                if spell_id:
                    all_spell_ids.add(int(spell_id))
                raw_entries.append((row.class_name, row.spec_name, node))

        # 批量预取 WowSpellSnapshot，消除 N+1
        snapshot_cache = {}
        if all_spell_ids:
            rows = (
                WowSpellSnapshot.objects
                .filter(spell_id__in=all_spell_ids)
                .order_by('spell_id', '-updated_at')
                .values_list('spell_id', 'name', 'name_zh')
            )
            seen = set()
            for sid, name, name_zh in rows:
                if sid in seen:
                    continue
                seen.add(sid)
                snapshot_cache[sid] = (name or '', name_zh or '')

        for class_name_val, spec_name_val, node in raw_entries:
            key = (
                class_name_val,
                spec_name_val,
                node.get('tree_type') or 'spec',
                node.get('node_id'),
                node.get('spell_id'),
            )
            defaults = self._build_defaults(node, source, snapshot_cache)
            if key not in merged_nodes:
                merged_nodes[key] = defaults
                continue

            current = merged_nodes[key]
            for field, value in defaults.items():
                if value in (None, '', []):
                    continue
                if current.get(field) in (None, '', []):
                    current[field] = value

        updated = 0
        for key, defaults in merged_nodes.items():
            meta, created = WowTalentNodeMetadata.objects.get_or_create(
                class_name=key[0],
                spec_name=key[1],
                tree_type=key[2],
                node_id=key[3],
                spell_id=key[4],
                defaults=defaults,
            )
            if not created:
                changed = False
                for field, value in defaults.items():
                    if value in (None, '', []):
                        continue
                    if getattr(meta, field) in (None, '', []):
                        setattr(meta, field, value)
                        changed = True
                if changed:
                    meta.last_updated = timezone.now()
                    meta.save()
            updated += 1
        return updated

    def _reclassify_tree_types(self, class_name=''):
        class_names = [class_name] if class_name else list(CLASS_SPEC_MAP.keys())
        for current_class in class_names:
            expected_specs = set(CLASS_SPEC_MAP.get(current_class, []))
            if not expected_specs:
                continue

            rows = list(
                WowTalentNodeMetadata.objects.filter(class_name=current_class)
                .exclude(tree_type='hero')
                .values('id', 'spec_name', 'node_id', 'spell_id', 'talent_id', 'tree_type')
            )
            actual_specs = {row['spec_name'] for row in rows}
            if not expected_specs.issubset(actual_specs):
                continue

            shared_map = {}
            for row in rows:
                key = row['node_id'] or row['talent_id'] or row['spell_id']
                if not key:
                    continue
                shared_map.setdefault(key, set()).add(row['spec_name'])

            class_keys = {
                key for key, spec_names in shared_map.items()
                if expected_specs.issubset(spec_names)
            }
            if not class_keys:
                continue

            updated = 0
            deleted = 0
            for row in rows:
                key = row['node_id'] or row['talent_id'] or row['spell_id']
                target_tree_type = 'class' if key in class_keys else 'spec'
                if row['tree_type'] == target_tree_type:
                    continue
                if target_tree_type == 'class':
                    existing_class = WowTalentNodeMetadata.objects.filter(
                        class_name=current_class, spec_name=row['spec_name'],
                        tree_type='class',
                        node_id=row['node_id'], spell_id=row['spell_id'],
                    ).exclude(id=row['id']).first()
                    if existing_class:
                        WowTalentNodeMetadata.objects.filter(id=row['id']).delete()
                        deleted += 1
                        continue
                try:
                    WowTalentNodeMetadata.objects.filter(id=row['id']).update(
                        tree_type=target_tree_type,
                        last_updated=timezone.now(),
                    )
                except Exception:
                    WowTalentNodeMetadata.objects.filter(id=row['id']).delete()
                    deleted += 1
                    continue
                updated += 1
            if updated:
                self.stdout.write(f'[{current_class}] 重分类 {updated} 条天赋树归属')

    @staticmethod
    def _build_defaults(node, source, snapshot_cache=None):
        snapshot = None
        spell_id = node.get('spell_id')
        if spell_id and snapshot_cache:
            cached = snapshot_cache.get(int(spell_id))
            if cached:
                snapshot = type('Snapshot', (), {'name': cached[0], 'name_zh': cached[1]})()
        elif spell_id:
            snapshot = WowSpellSnapshot.objects.filter(spell_id=spell_id).order_by('-updated_at').first()

        name = node.get('name', '')
        if name.startswith('技能ID ') or name == '未命名天赋':
            name = ''

        return {
            'talent_id': node.get('talent_id'),
            'name': name or (snapshot.name if snapshot else ''),
            'name_zh': snapshot.name_zh if snapshot else '',
            'icon': node.get('icon', ''),
            'row': node.get('row'),
            'column': node.get('column'),
            'max_points': node.get('max_points') or 1,
            'parents_json': list(node.get('parents') or node.get('parents_json') or []),
            'source': source,
            'last_updated': timezone.now(),
        }
