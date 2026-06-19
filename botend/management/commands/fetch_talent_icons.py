# -*- coding: utf-8 -*-
"""
批量从 wago.tools 获取天赋图标名称并更新数据库。

使用 TraitDefinition.OverrideIcon (FileDataID) 查询 wago.tools API，
解析出图标名称后更新 WowTalentNodeMetadata.icon 字段。
"""
import csv
import html
import json
import os
import re
import time

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.models import WowTalentNodeMetadata


class Command(BaseCommand):
    help = '批量从 wago.tools 获取天赋图标名称'

    def add_arguments(self, parser):
        parser.add_argument('--dump-dir', default='.cache/wago_db2_dumps/latest',
                            help='DB2 dump 目录')
        parser.add_argument('--limit', type=int, default=0, help='最多处理多少个节点，0=不限制')
        parser.add_argument('--delay', type=float, default=0.5, help='每次请求延迟(秒)')
        parser.add_argument('--batch-size', type=int, default=100, help='批量更新大小')

    def handle(self, *args, **options):
        dump_dir = options['dump_dir']
        limit = options['limit']
        delay = options['delay']
        batch_size = options['batch_size']

        # 1. 加载 TraitDefinition → OverrideIcon 映射
        self.stdout.write('加载 TraitDefinition...')
        def_icon_map = {}  # def_id → file_data_id
        with open(os.path.join(dump_dir, 'TraitDefinition.csv')) as f:
            for row in csv.DictReader(f):
                did = int(row['ID'])
                icon_id = int(row.get('OverrideIcon', 0) or 0)
                if icon_id > 0:
                    def_icon_map[did] = icon_id
        self.stdout.write(f'  TraitDefinition with OverrideIcon: {len(def_icon_map)}')

        # 2. 加载 TraitNodeEntry → TraitDefinitionID 映射
        self.stdout.write('加载 TraitNodeEntry...')
        entry_def_map = {}  # entry_id → def_id
        with open(os.path.join(dump_dir, 'TraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                eid = int(row['ID'])
                did = int(row['TraitDefinitionID'])
                entry_def_map[eid] = did
        self.stdout.write(f'  TraitNodeEntry: {len(entry_def_map)}')

        # 3. 加载 SpellMisc → SpellIconFileDataID 映射
        self.stdout.write('加载 SpellMisc...')
        spell_icon_map = {}  # spell_id → file_data_id
        spell_misc_path = os.path.join(dump_dir, 'SpellMisc.csv')
        if os.path.exists(spell_misc_path):
            with open(spell_misc_path) as f:
                for row in csv.DictReader(f):
                    sid = int(row.get('SpellID', 0) or 0)
                    icon_id = int(row.get('SpellIconFileDataID', 0) or 0)
                    if sid > 0 and icon_id > 0:
                        spell_icon_map[sid] = icon_id
        self.stdout.write(f'  SpellMisc with icon: {len(spell_icon_map)}')

        # 4. 获取需要图标的节点
        queryset = WowTalentNodeMetadata.objects.filter(icon='')
        if limit:
            queryset = queryset[:limit]
        nodes = list(queryset)
        self.stdout.write(f'需要获取图标的节点: {len(nodes)}')

        # 5. 收集所有需要查询的 FileDataID
        file_data_ids = set()  # file_data_id → [(node_id, ...)]
        node_icon_map = {}  # node_id → file_data_id

        for node in nodes:
            file_data_id = None

            # 方法1: 通过 node_id → TraitNodeEntry → TraitDefinition → OverrideIcon
            if node.node_id and node.node_id in entry_def_map:
                def_id = entry_def_map[node.node_id]
                if def_id in def_icon_map:
                    file_data_id = def_icon_map[def_id]

            # 方法2: 通过 display_spell_id → SpellMisc → SpellIconFileDataID
            if not file_data_id and node.display_spell_id:
                if node.display_spell_id in spell_icon_map:
                    file_data_id = spell_icon_map[node.display_spell_id]

            # 方法3: 通过 spell_id → SpellMisc
            if not file_data_id and node.spell_id:
                if node.spell_id in spell_icon_map:
                    file_data_id = spell_icon_map[node.spell_id]

            if file_data_id:
                file_data_ids.add(file_data_id)
                node_icon_map[node.id] = file_data_id

        self.stdout.write(f'需要查询的 FileDataID: {len(file_data_ids)}')

        # 6. 批量查询 wago.tools 获取图标名
        self.stdout.write('开始查询 wago.tools...')
        icon_cache = {}  # file_data_id → icon_name
        total = len(file_data_ids)
        queried = 0

        for file_data_id in sorted(file_data_ids):
            if file_data_id in icon_cache:
                continue

            url = f'https://wago.tools/files?search={file_data_id}'
            try:
                r = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
                icon_cache[file_data_id] = self._extract_icon_name(r.text, file_data_id)
            except Exception as e:
                self.stderr.write(f'查询失败 FileDataID={file_data_id}: {e}')
                icon_cache[file_data_id] = ''

            queried += 1
            if queried % 10 == 0:
                self.stdout.write(f'  进度: {queried}/{total}')
            time.sleep(delay)

        self.stdout.write(f'查询完成: {len(icon_cache)} 个图标')

        # 7. 批量更新数据库
        self.stdout.write('更新数据库...')
        to_update = []
        updated = 0

        for node in nodes:
            file_data_id = node_icon_map.get(node.id)
            if not file_data_id:
                continue

            icon_name = icon_cache.get(file_data_id, '')
            if not icon_name:
                continue

            node.icon = icon_name
            to_update.append(node)
            updated += 1

            if len(to_update) >= batch_size:
                WowTalentNodeMetadata.objects.bulk_update(to_update, ['icon'])
                to_update = []
                self.stdout.write(f'  已更新 {updated} 条')

        if to_update:
            WowTalentNodeMetadata.objects.bulk_update(to_update, ['icon'])

        self.stdout.write(self.style.SUCCESS(
            f'完成: 更新 {updated} 个节点的图标'
        ))

    def _extract_icon_name(self, text, file_data_id):
        for raw in re.findall(r'filename&quot;:&quot;([^&]+\.blp)&quot;', text, re.I):
            icon_name = self._icon_name_from_path(raw)
            if icon_name:
                return icon_name

        page_match = re.search(r'data-page="([^"]+)"', text, re.S)
        if not page_match:
            return ''
        try:
            data = json.loads(html.unescape(page_match.group(1)))
        except Exception:
            return ''

        files = data.get('props', {}).get('files', {})
        rows = files.get('data') if isinstance(files, dict) else []
        for row in rows or []:
            try:
                row_fdid = int(row.get('fdid') or row.get('id') or 0)
            except Exception:
                row_fdid = 0
            if row_fdid and row_fdid != int(file_data_id):
                continue
            icon_name = self._icon_name_from_path(row.get('filename') or '')
            if icon_name:
                return icon_name
        return ''

    def _icon_name_from_path(self, raw):
        path = html.unescape(str(raw or '')).replace('\\/', '/').lower()
        if '/icons/' not in path:
            return ''
        base = os.path.basename(path)
        if not base.endswith('.blp'):
            return ''
        return base[:-4]
