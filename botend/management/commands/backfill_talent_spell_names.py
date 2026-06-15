# -*- coding: utf-8 -*-

import os
import re

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections
from django.db.models import Q
from django.utils import timezone

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowWagoMonitorState


HERO_SUBTREE_NAME_ZH = {
    'Shado-Pan': '影踪派',
    'Conduit of the Celestials': '天神御师',
}


class Command(BaseCommand):
    help = '使用 wago.tools DB2 回填天赋相关 spell 中文名，并同步到天赋元数据缓存'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._db2_filter_cache = {}
        self._icon_cache = {}
        self._db2_row_cache = {}

    def add_arguments(self, parser):
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--spec-name', default='', help='仅处理指定专精')
        parser.add_argument('--build', default='', help='显式指定 Wago build，例如 12.0.7.67525')
        parser.add_argument('--branch', default='wow', help='Wago branch，默认 wow')
        parser.add_argument('--limit', type=int, default=200, help='最多处理多少个 spell_id')
        parser.add_argument('--chunk-size', type=int, default=50, help='每批处理的 spell_id 数量')
        parser.add_argument('--refresh-tree-type', action='store_true', help='强制重算 tree_type/row/column')

    def handle(self, *args, **options):
        class_name = options['class_name']
        spec_name = options['spec_name']
        branch = options['branch']
        limit = max(0, int(options['limit']))
        chunk_size = max(1, min(int(options['chunk_size']), 200))
        refresh_tree_type = bool(options.get('refresh_tree_type'))
        build = (options['build'] or '').strip() or self._guess_build(branch)

        if not build:
            raise CommandError('无法推断可用 build，请使用 --build 显式指定')

        monitor = WagoSkillDiffMonitor(None, None)
        if refresh_tree_type:
            queryset = WowTalentNodeMetadata.objects.exclude(spell_id__isnull=True)
        else:
            queryset = WowTalentNodeMetadata.objects.filter(
                Q(display_spell_id__isnull=True) |
                Q(name='') |
                Q(name='未命名天赋') |
                Q(name__startswith='技能ID ') |
                Q(icon='') |
                Q(row__isnull=True) |
                Q(column__isnull=True) |
                Q(parents_json=[])
            ).exclude(spell_id__isnull=True)
        if class_name:
            queryset = queryset.filter(class_name=class_name)
        if spec_name:
            queryset = queryset.filter(spec_name=spec_name)

        if limit:
            queryset = queryset.order_by('id')[:limit]
        else:
            queryset = queryset.order_by('id')
        rows = list(queryset)
        if not rows:
            self.stdout.write(self.style.WARNING('没有找到需要处理的天赋节点'))
            return

        self.stdout.write(f'使用 build {build} 解析 {len(rows)} 条天赋节点')

        # 预取 TraitNodeEntry / TraitDefinition，减少每条记录的 DB2 请求次数
        raw_ids = [
            int(r.node_id or r.talent_id or r.spell_id or 0)
            for r in rows
            if int(r.node_id or r.talent_id or r.spell_id or 0) > 0
        ]
        entry_map = monitor._fetch_db2_rows_by_ids_requests('TraitNodeEntry', build, raw_ids)
        definition_ids = []
        for entry in entry_map.values():
            try:
                definition_ids.append(int(entry.get('TraitDefinitionID') or 0))
            except Exception:
                continue
        definition_ids = [x for x in definition_ids if x > 0]
        definition_map = monitor._fetch_db2_rows_by_ids_requests('TraitDefinition', build, definition_ids) if definition_ids else {}
        self._db2_row_cache[(build, 'TraitNodeEntry')] = entry_map
        self._db2_row_cache[(build, 'TraitDefinition')] = definition_map

        snapshot_spell_ids = {}
        updated = 0
        for index, row in enumerate(rows, start=1):
            if index == 1 or index % 20 == 0:
                close_old_connections()
            resolved = self._resolve_metadata_row(monitor, build, row)
            if not resolved:
                continue
            spell_id = resolved.get('display_spell_id')
            name_zh = (resolved.get('name_zh') or '').strip()
            if not spell_id or not name_zh:
                pass
            else:
                snapshot_spell_ids[int(spell_id)] = name_zh

            now = timezone.now()
            target_tree_type = (resolved.get('tree_type') or row.tree_type or 'spec').strip() or 'spec'
            conflict_row = self._find_conflict_row(row, target_tree_type)
            if conflict_row:
                merged = self._merge_metadata_values(conflict_row, row, resolved)
                conflict_changed = False
                for field, value in merged.items():
                    if field == 'parents_json':
                        if list(conflict_row.parents_json or []) != list(value or []):
                            conflict_row.parents_json = list(value or [])
                            conflict_changed = True
                        continue
                    if value in (None, '', []):
                        continue
                    if getattr(conflict_row, field) != value:
                        setattr(conflict_row, field, value)
                        conflict_changed = True
                if conflict_changed:
                    conflict_row.last_updated = now
                    self._retry_db_write(
                        conflict_row.save,
                        update_fields=['display_spell_id', 'name', 'name_zh', 'icon', 'row', 'column', 'max_points', 'parents_json', 'tree_type', 'last_updated'],
                    )
                    updated += 1
                self._retry_db_write(row.delete)
                if index <= 10 or index % 25 == 0 or index == len(rows):
                    self.stdout.write(
                        f'[{index}/{len(rows)}] dedup merged id={row.id} -> {conflict_row.id} '
                        f'node_id={row.node_id} spell_id={row.spell_id} tree_type={target_tree_type}'
                    )
                continue

            changed = False
            for field in ['display_spell_id', 'name', 'name_zh', 'row', 'column', 'max_points']:
                value = resolved.get(field)
                if value in (None, '', []):
                    continue
                if getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True
            parents_value = resolved.get('parents_json')
            if parents_value is not None:
                merged_parents = sorted(
                    {
                        self._coerce_int(parent_id, 0) or 0
                        for parent_id in list(row.parents_json or []) + list(parents_value or [])
                        if self._coerce_int(parent_id, 0)
                    }
                )
                if list(row.parents_json or []) != merged_parents:
                    row.parents_json = merged_parents
                    changed = True
            tree_type_value = (resolved.get('tree_type') or '').strip()
            if tree_type_value and row.tree_type != tree_type_value:
                row.tree_type = tree_type_value
                changed = True
            icon_value = (resolved.get('icon') or '').strip()
            if icon_value and row.icon != icon_value:
                row.icon = icon_value
                changed = True
            if changed:
                row.last_updated = now
                self._retry_db_write(
                    row.save,
                    update_fields=['display_spell_id', 'name', 'name_zh', 'icon', 'row', 'column', 'max_points', 'parents_json', 'tree_type', 'last_updated'],
                )
                updated += 1

            if index <= 10 or index % 25 == 0 or index == len(rows):
                self.stdout.write(
                    f'[{index}/{len(rows)}] updated={updated} '
                    f'node_id={row.node_id} spell_id={row.spell_id} '
                    f'row={resolved.get("row")} col={resolved.get("column")} '
                    f'parents={len(resolved.get("parents_json") or [])}'
                )

        for spell_id, name_zh in snapshot_spell_ids.items():
            self._retry_db_write(
                WowSpellSnapshot.objects.update_or_create,
                branch=branch,
                locale=monitor.locale,
                spell_id=int(spell_id),
                defaults={
                    'name_zh': (name_zh or '')[:255],
                    'snapshot_build': build,
                    'updated_at': now,
                }
            )

        self.stdout.write(self.style.SUCCESS(
            f'已通过 trait 映射回填 {len(snapshot_spell_ids)} 个真实 spell 名称，更新 {updated} 条天赋元数据'
        ))

    @staticmethod
    def _find_conflict_row(row, target_tree_type):
        def _query():
            return WowTalentNodeMetadata.objects.filter(
                class_name=row.class_name,
                spec_name=row.spec_name,
                tree_type=target_tree_type,
                node_id=row.node_id,
                spell_id=row.spell_id,
            ).exclude(id=row.id).first()

        close_old_connections()
        try:
            return _query()
        except OperationalError:
            close_old_connections()
            return _query()

    def _resolve_metadata_row(self, monitor, build, row):
        raw_id = row.node_id or row.talent_id or row.spell_id
        if not raw_id:
            return {}

        existing_display_spell_id = int(row.display_spell_id or 0)
        layout = self._resolve_trait_layout(monitor, build, raw_id)
        layout_row = layout.get('row')
        layout_column = layout.get('column')
        resolved = {
            'row': layout_row if layout_row is not None else row.row,
            'column': layout_column if layout_column is not None else row.column,
            'max_points': row.max_points or 1,
            'tree_type': layout.get('tree_type') or row.tree_type or 'spec',
            'parents_json': self._resolve_trait_parents(monitor, build, raw_id),
        }

        entry = (self._db2_row_cache.get((build, 'TraitNodeEntry')) or {}).get(int(raw_id)) or \
            monitor._fetch_db2_row_by_id_requests('TraitNodeEntry', build, raw_id)
        if entry:
            definition_id = int(entry.get('TraitDefinitionID') or 0)
            max_ranks = int(entry.get('MaxRanks') or 1)
            subtree_id = int(entry.get('TraitSubTreeID') or 0)
            definition = (self._db2_row_cache.get((build, 'TraitDefinition')) or {}).get(int(definition_id)) or \
                (monitor._fetch_db2_row_by_id_requests('TraitDefinition', build, definition_id) if definition_id else {})
            resolved['max_points'] = max_ranks
            display_spell_id = int(
                definition.get('VisibleSpellID')
                or definition.get('SpellID')
                or definition.get('OverridesSpellID')
                or 0
            )
            if display_spell_id > 0:
                resolved['display_spell_id'] = display_spell_id
            elif existing_display_spell_id > 0:
                resolved['display_spell_id'] = existing_display_spell_id

            if row.name_zh and row.icon and row.display_spell_id and layout:
                resolved['row'] = layout_row if layout_row is not None else resolved.get('row')
                resolved['column'] = layout_column if layout_column is not None else resolved.get('column')
                resolved['name'] = row.name_zh or row.name
                resolved['name_zh'] = row.name_zh
                resolved['icon'] = row.icon
                return resolved

            if display_spell_id > 0:
                name_zh = (row.name_zh or '').strip() or self._resolve_spell_name(monitor, build, display_spell_id)
                if name_zh:
                    resolved['row'] = layout_row if layout_row is not None else resolved.get('row')
                    resolved['column'] = layout_column if layout_column is not None else resolved.get('column')
                    resolved['name'] = name_zh
                    resolved['name_zh'] = name_zh
                    resolved['icon'] = (row.icon or '').strip() or self._resolve_spell_icon(monitor, build, display_spell_id, definition)
                    return resolved

            subtree_name = self._resolve_subtree_name(monitor, build, subtree_id)
            if subtree_name:
                resolved['row'] = layout_row if layout_row is not None else resolved.get('row')
                resolved['column'] = layout_column if layout_column is not None else resolved.get('column')
                resolved['name'] = subtree_name
                resolved['name_zh'] = subtree_name
                return resolved

        direct_spell_id = int(row.spell_id or 0)
        if direct_spell_id > 0:
            resolved['display_spell_id'] = existing_display_spell_id or direct_spell_id
            name_zh = (row.name_zh or '').strip() or self._resolve_spell_name(monitor, build, direct_spell_id)
            if name_zh:
                resolved['row'] = layout_row if layout_row is not None else resolved.get('row')
                resolved['column'] = layout_column if layout_column is not None else resolved.get('column')
                resolved['name'] = name_zh
                resolved['name_zh'] = name_zh
                resolved['icon'] = (row.icon or '').strip() or self._resolve_spell_icon(monitor, build, direct_spell_id, {})
            return resolved
        return resolved if layout else {}

    def _resolve_trait_parents(self, monitor, build, trait_node_entry_id):
        trait_node_id = self._resolve_trait_node_id(monitor, build, trait_node_entry_id)
        if not trait_node_id:
            return []

        current_node = monitor._fetch_db2_row_by_id_requests('TraitNode', build, trait_node_id) or {}
        current_y = self._coerce_int(current_node.get('PosY'))
        current_x = self._coerce_int(current_node.get('PosX'))

        parent_entry_ids = []
        seen = set()
        for field_name, other_field in (
            ('LeftTraitNodeID', 'RightTraitNodeID'),
            ('RightTraitNodeID', 'LeftTraitNodeID'),
        ):
            edges = self._fetch_db2_rows_by_filter(
                monitor,
                'TraitEdge',
                build,
                {field_name: trait_node_id},
            )
            for edge in edges:
                other_trait_node_id = self._coerce_int(edge.get(other_field))
                if not other_trait_node_id or other_trait_node_id == trait_node_id:
                    continue
                other_node = monitor._fetch_db2_row_by_id_requests('TraitNode', build, other_trait_node_id) or {}
                other_y = self._coerce_int(other_node.get('PosY'))
                other_x = self._coerce_int(other_node.get('PosX'))
                if other_y is None or current_y is None:
                    continue
                if other_y > current_y:
                    continue
                if other_y == current_y and other_x is not None and current_x is not None and other_x >= current_x:
                    continue
                parent_entry_id = self._resolve_primary_entry_id_for_trait_node(monitor, build, other_trait_node_id)
                if not parent_entry_id or parent_entry_id in seen:
                    continue
                seen.add(parent_entry_id)
                parent_entry_ids.append(parent_entry_id)
        return parent_entry_ids

    def _resolve_trait_node_id(self, monitor, build, trait_node_entry_id):
        links = self._fetch_db2_rows_by_filter(
            monitor,
            'TraitNodeXTraitNodeEntry',
            build,
            {'TraitNodeEntryID': trait_node_entry_id},
        )
        if not links:
            return 0
        link = links[0] if isinstance(links[0], dict) else {}
        return self._coerce_int(link.get('TraitNodeID') or link.get('TraitNode') or link.get('TraitNodeId'), 0) or 0

    def _resolve_primary_entry_id_for_trait_node(self, monitor, build, trait_node_id):
        links = self._fetch_db2_rows_by_filter(
            monitor,
            'TraitNodeXTraitNodeEntry',
            build,
            {'TraitNodeID': trait_node_id},
        )
        if not links:
            return 0
        entry_ids = sorted(
            {
                self._coerce_int(link.get('TraitNodeEntryID') or link.get('TraitNodeEntry') or link.get('TraitNodeEntryId'), 0) or 0
                for link in links
            }
        )
        for entry_id in entry_ids:
            if entry_id > 0:
                return entry_id
        return 0

    def _merge_metadata_values(self, target_row, source_row, resolved):
        merged_parents = sorted(
            {
                self._coerce_int(parent_id, 0) or 0
                for parent_id in list(target_row.parents_json or []) + list(source_row.parents_json or []) + list(resolved.get('parents_json') or [])
                if self._coerce_int(parent_id, 0)
            }
        )
        return {
            'display_spell_id': resolved.get('display_spell_id') or target_row.display_spell_id or source_row.display_spell_id,
            'name': resolved.get('name') or target_row.name or source_row.name,
            'name_zh': resolved.get('name_zh') or target_row.name_zh or source_row.name_zh,
            'icon': resolved.get('icon') or target_row.icon or source_row.icon,
            'row': resolved.get('row') if resolved.get('row') is not None else (target_row.row if target_row.row is not None else source_row.row),
            'column': resolved.get('column') if resolved.get('column') is not None else (target_row.column if target_row.column is not None else source_row.column),
            'max_points': resolved.get('max_points') or target_row.max_points or source_row.max_points,
            'parents_json': merged_parents,
            'tree_type': (resolved.get('tree_type') or target_row.tree_type or source_row.tree_type or 'spec').strip() or 'spec',
        }

    @staticmethod
    def _resolve_spell_name(monitor, build, spell_id):
        row = monitor._fetch_db2_row_by_id_requests('SpellName', build, spell_id, locale_override=monitor.name_locale)
        name = (row.get('Name_lang') or '').strip()
        if name:
            return name
        return (monitor._fetch_spell_name_wowhead_cn(spell_id) or '').strip()

    def _resolve_spell_icon(self, monitor, build, spell_id, definition):
        override_icon = int((definition or {}).get('OverrideIcon') or 0)
        if override_icon > 0:
            icon_name = self._resolve_icon_name_by_filedata(override_icon)
            if icon_name:
                return icon_name

        misc = monitor._fetch_spellmisc_by_spellid(build, spell_id)
        icon_file_data_id = int((misc or {}).get('SpellIconFileDataID') or 0)
        if icon_file_data_id <= 0:
            icon_file_data_id = int((misc or {}).get('ActiveIconFileDataID') or 0)
        if icon_file_data_id <= 0:
            return ''
        return self._resolve_icon_name_by_filedata(icon_file_data_id)

    @staticmethod
    def _coerce_int(value, default=None):
        try:
            return int(str(value).strip())
        except Exception:
            return default

    def _resolve_trait_layout(self, monitor, build, trait_node_entry_id):
        links = self._fetch_db2_rows_by_filter(
            monitor,
            'TraitNodeXTraitNodeEntry',
            build,
            {'TraitNodeEntryID': trait_node_entry_id},
        )
        if not links:
            return {}

        link = links[0] if isinstance(links[0], dict) else {}
        trait_node_id = self._coerce_int(link.get('TraitNodeID') or link.get('TraitNode') or link.get('TraitNodeId'))
        if not trait_node_id:
            return {}

        node = monitor._fetch_db2_row_by_id_requests('TraitNode', build, trait_node_id)
        if not node:
            return {}

        pos_x = self._coerce_int(node.get('PosX'))
        pos_y = self._coerce_int(node.get('PosY'))
        if pos_x is None and pos_y is None:
            return {}
        node_subtree_id = self._coerce_int(node.get('TraitSubTreeID'), 0) or 0
        return {
            'row': pos_y,
            'column': pos_x,
            'tree_type': 'hero' if node_subtree_id > 0 else 'spec',
        }

    def _fetch_db2_rows_by_filter(self, monitor, table, build, filters, locale_override=None):
        normalized_filters = tuple(sorted(
            (str(key), str(value).strip())
            for key, value in (filters or {}).items()
            if str(value).strip()
        ))
        cache_key = (table, build, locale_override or '', normalized_filters)
        if cache_key in self._db2_filter_cache:
            return self._db2_filter_cache[cache_key]

        use_locale = (locale_override or monitor.locale or '').strip() or 'enUS'
        url = f'https://wago.tools/db2/{table}?build={build}&locale={use_locale}'
        for key, value in normalized_filters:
            url += f'&filter[{key}]=exact:{value}'
        try:
            response = requests.get(url, timeout=max(30, monitor.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
        except Exception:
            self._db2_filter_cache[cache_key] = []
            return []
        if response.status_code != 200:
            self._db2_filter_cache[cache_key] = []
            return []
        try:
            text = response.content.decode('utf-8', 'replace')
        except Exception:
            text = response.text or ''

        props = monitor._extract_inertia_props(text or '')
        data = []
        if 'entries' in props:
            entries = props.get('entries') or {}
            data = entries.get('data') if isinstance(entries, dict) else (entries if isinstance(entries, list) else [])
        elif 'data' in props:
            payload = props.get('data')
            data = payload.get('data') if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        rows = data if isinstance(data, list) else []
        self._db2_filter_cache[cache_key] = rows
        return rows

    def _resolve_subtree_name(self, monitor, build, subtree_id):
        subtree_id = self._coerce_int(subtree_id, 0) or 0
        if subtree_id <= 0:
            return ''
        row = monitor._fetch_db2_row_by_id_requests('TraitSubTree', build, subtree_id)
        raw_name = (row.get('Name_lang') or '').strip()
        if not raw_name:
            return ''
        return HERO_SUBTREE_NAME_ZH.get(raw_name, raw_name)

    def _resolve_icon_name_by_filedata(self, file_data_id):
        if not file_data_id:
            return ''
        file_data_id = int(file_data_id)
        if file_data_id in self._icon_cache:
            return self._icon_cache[file_data_id]
        url = f'https://wago.tools/files?search={int(file_data_id)}'
        try:
            html = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'}).text
        except Exception:
            self._icon_cache[file_data_id] = ''
            return ''
        matches = re.findall(r'filename&quot;:&quot;([^&]+\.blp)&quot;', html, re.I)
        for raw in matches:
            path = raw.replace('\\/', '/').lower()
            if '/icons/' not in path:
                continue
            base = os.path.basename(path)
            if not base.endswith('.blp'):
                continue
            self._icon_cache[file_data_id] = base[:-4]
            return self._icon_cache[file_data_id]
        self._icon_cache[file_data_id] = ''
        return ''

    @staticmethod
    def _retry_db_write(func, *args, **kwargs):
        close_old_connections()
        try:
            return func(*args, **kwargs)
        except OperationalError:
            close_old_connections()
            return func(*args, **kwargs)

    @staticmethod
    def _guess_build(branch):
        state = WowWagoMonitorState.objects.filter(branch=branch, is_active=True).order_by('-updated_at').first()
        if state and (state.build or '').strip():
            return state.build.strip()

        latest = WowSpellSnapshot.objects.exclude(snapshot_build='').order_by('-updated_at').first()
        if latest and (latest.snapshot_build or '').strip():
            return latest.snapshot_build.strip()
        return ''
