# -*- coding: utf-8 -*-
"""Repair PTR talent metadata from local DB2 dumps and Wowhead fallbacks."""
import csv
import html
import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion


BAD_DESCRIPTION_TOKENS = ('$', '@spell', '<', '|c', '|C', '|r', '|R')
DESCRIPTION_FIXUPS = {
    360827: '用数枚高爆龙鳞保护一名盟友，使其护甲值提高。对该目标的近战攻击会引爆一枚鳞片，对其附近的敌人造成熔火伤害。该伤害每数秒只能触发一次。同一时间只能对一个目标施放炽火龙鳞。若敌人的目标已经拥有此效果，则会对其施放。',
    404542: '十字军打击会替换你的自动攻击，造成物理伤害，但你的自动攻击速度下降20%。',
}
DEFAULT_FIELDS = [
    'talent_id',
    'display_spell_id',
    'name',
    'name_zh',
    'icon',
    'row',
    'column',
    'max_points',
    'parents_json',
    'description',
    'description_zh',
    'db2_subtree_id',
    'db2_tree_id',
    'flags',
    'last_updated',
]
GENERIC_ICON_NAMES = {
    'trade_engineering',
    'inv_10_jewelcrafting_gem2standard_uncut_green',
}


class Command(BaseCommand):
    help = 'Repair visible PTR talent metadata fields from DB2 CSV and cache-backed Wowhead fallbacks.'

    def add_arguments(self, parser):
        parser.add_argument('--version-key', default='ptr-12.1.0')
        parser.add_argument('--dump-dir', default='')
        parser.add_argument('--cache-dir', default='.cache')
        parser.add_argument('--class-name', default='')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--skip-wowhead', action='store_true')
        parser.add_argument('--refresh-icons', action='store_true', help='Fetch/check Wowhead icons for all visible display spell ids')
        parser.add_argument('--workers', type=int, default=12)
        parser.add_argument('--limit', type=int, default=0, help='Limit DB rows for debugging')

    def handle(self, *args, **options):
        version_key = (options.get('version_key') or '').strip()
        try:
            talent_version = WowTalentVersion.objects.get(key=version_key)
        except WowTalentVersion.DoesNotExist as exc:
            raise CommandError(f'WowTalentVersion not found: {version_key}') from exc

        dump_dir = (options.get('dump_dir') or '').strip() or talent_version.source_dir or '.cache/wago_db2_dumps/ptr'
        if not os.path.isdir(dump_dir):
            raise CommandError(f'DB2 dump dir not found: {dump_dir}')

        cache_dir = Path(options.get('cache_dir') or '.cache')
        dry_run = bool(options.get('dry_run'))
        skip_wowhead = bool(options.get('skip_wowhead'))
        workers = max(1, int(options.get('workers') or 1))
        class_name = (options.get('class_name') or '').strip()

        self.stdout.write(f'Loading DB2 dump: {dump_dir}')
        db2 = self._load_db2(dump_dir)

        qs = WowTalentNodeMetadata.objects.filter(talent_version=talent_version).exclude(tree_type='hero_anchor')
        if class_name:
            qs = qs.filter(class_name=class_name)
        if options.get('limit'):
            qs = qs[:int(options['limit'])]
        rows = list(qs)
        self.stdout.write(f'Visible metadata rows: {len(rows)}')

        expected = {row.id: self._expected_for_row(row, db2) for row in rows}
        icon_spell_ids = self._collect_icon_spell_ids(rows, expected, refresh_all=bool(options.get('refresh_icons')))
        icon_cache = self._load_json(cache_dir / f'wowhead_spell_icon_cache_{version_key}.json')
        desc_cache = self._load_json(cache_dir / f'wowhead_spell_desc_cache_{version_key}.json')

        if not skip_wowhead:
            self._fill_icon_cache(icon_spell_ids, icon_cache, cache_dir / f'wowhead_spell_icon_cache_{version_key}.json', workers)
            bad_desc_ids = self._collect_bad_description_spell_ids(rows)
            self._fill_desc_cache(bad_desc_ids, desc_cache, cache_dir / f'wowhead_spell_desc_cache_{version_key}.json', workers)

        updates = []
        stats = Counter()
        samples = []
        now = timezone.now()
        for row in rows:
            values = dict(expected[row.id])
            spell_id = self._coerce_int(values.get('display_spell_id') or row.display_spell_id or row.spell_id)
            icon_data = icon_cache.get(str(spell_id)) or {}
            wowhead_icon = (icon_data.get('icon') or '').strip()
            trusted_wowhead_icon = wowhead_icon and self._is_trusted_wowhead_icon(icon_data, row)
            db2_icon = values.get('icon') or ''
            if db2_icon and db2_icon not in GENERIC_ICON_NAMES:
                values['icon'] = db2_icon
            elif trusted_wowhead_icon:
                values['icon'] = wowhead_icon
            elif row.icon:
                if wowhead_icon and row.icon == wowhead_icon and db2_icon:
                    values['icon'] = db2_icon
                else:
                    values.pop('icon', None)
            elif db2_icon:
                values['icon'] = db2_icon
            else:
                values.pop('icon', None)

            desc = (desc_cache.get(str(spell_id)) or {}).get('description_zh', '').strip()
            if spell_id in DESCRIPTION_FIXUPS:
                desc = DESCRIPTION_FIXUPS[spell_id]
            current_desc = row.description_zh or ''
            if desc and not self._has_bad_description_tokens(desc):
                if not current_desc or self._has_bad_description_tokens(current_desc):
                    values['description_zh'] = desc
            elif current_desc and not self._has_bad_description_tokens(current_desc):
                values.pop('description_zh', None)

            current_desc_en = row.description or ''
            candidate_desc_en = values.get('description') or ''
            if current_desc_en and not self._has_bad_description_tokens(current_desc_en):
                values.pop('description', None)
            elif not candidate_desc_en or self._has_bad_description_tokens(candidate_desc_en):
                values.pop('description', None)

            candidate_desc_zh = values.get('description_zh') or ''
            if not candidate_desc_zh or self._has_bad_description_tokens(candidate_desc_zh):
                values.pop('description_zh', None)

            changed = []
            for field, value in values.items():
                if field in ('name', 'name_zh', 'icon', 'description_zh') and not value:
                    continue
                current = getattr(row, field)
                if field == 'parents_json':
                    current_cmp = list(current or [])
                    value_cmp = list(value or [])
                    if current_cmp != value_cmp:
                        setattr(row, field, value_cmp)
                        changed.append(field)
                elif current != value:
                    setattr(row, field, value)
                    changed.append(field)
            if changed:
                row.last_updated = now
                updates.append(row)
                for field in changed:
                    stats[field] += 1
                if len(samples) < 30:
                    samples.append({
                        'node': [row.class_name, row.spec_name, row.tree_type, row.node_id, row.spell_id],
                        'changed': changed,
                        'name_zh': row.name_zh,
                        'icon': row.icon,
                    })

        self.stdout.write(f'Planned updates: {len(updates)} {dict(stats)}')
        if samples:
            self.stdout.write('Samples: ' + json.dumps(samples, ensure_ascii=False)[:4000])

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN: no rows written'))
            return

        for start in range(0, len(updates), 500):
            WowTalentNodeMetadata.objects.bulk_update(updates[start:start + 500], DEFAULT_FIELDS, batch_size=500)
        self._write_spell_snapshots(desc_cache, talent_version.current_build or '', now)
        self.stdout.write(self.style.SUCCESS(f'Updated {len(updates)} visible PTR talent metadata rows'))

    def _load_db2(self, dump_dir):
        data = {
            'trait_nodes': self._map_csv(dump_dir, 'TraitNode.csv'),
            'entries': self._map_csv(dump_dir, 'TraitNodeEntry.csv'),
            'defs_zh': self._map_csv(dump_dir, 'TraitDefinition_zhCN.csv'),
            'defs_en': self._map_csv(dump_dir, 'TraitDefinition_enUS.csv'),
            'spell_names_zh': self._map_csv(dump_dir, 'SpellName_zhCN.csv'),
            'spell_names_en': self._map_csv(dump_dir, 'SpellName_enUS.csv'),
            'spells_zh': self._map_csv(dump_dir, 'Spell_zhCN.csv'),
            'spells_en': self._map_csv(dump_dir, 'Spell_enUS.csv') if os.path.exists(os.path.join(dump_dir, 'Spell_enUS.csv')) else {},
            'fdid_to_icon': self._load_icon_cache(dump_dir),
            'spell_to_icon': {},
            'entry_to_node': {},
            'node_entries': defaultdict(list),
            'edges': defaultdict(list),
        }
        for row in self._read_csv(dump_dir, 'TraitNodeXTraitNodeEntry.csv'):
            node_id = self._coerce_int(row.get('TraitNodeID'))
            entry_id = self._coerce_int(row.get('TraitNodeEntryID'))
            if node_id and entry_id:
                data['entry_to_node'][entry_id] = node_id
                data['node_entries'][node_id].append(entry_id)
        for row in self._read_csv(dump_dir, 'TraitEdge.csv'):
            left_id = self._coerce_int(row.get('LeftTraitNodeID'))
            right_id = self._coerce_int(row.get('RightTraitNodeID'))
            if left_id and right_id:
                data['edges'][right_id].append(left_id)
        for row in self._read_csv(dump_dir, 'spell_icon_map.csv'):
            spell_id = self._coerce_int(row.get('SpellID'))
            file_data_id = self._coerce_int(row.get('FileDataID'))
            icon = data['fdid_to_icon'].get(file_data_id, '')
            if spell_id and icon:
                data['spell_to_icon'][spell_id] = icon
        return data

    def _expected_for_row(self, row, db2):
        entry_id = self._coerce_int(row.node_id)
        trait_node_id = db2['entry_to_node'].get(entry_id) or self._coerce_int(row.talent_id)
        trait_node = db2['trait_nodes'].get(trait_node_id) or {}
        entry = db2['entries'].get(entry_id) or {}
        definition_id = self._coerce_int(entry.get('TraitDefinitionID'))
        definition_zh = db2['defs_zh'].get(definition_id) or {}
        definition_en = db2['defs_en'].get(definition_id) or {}

        spell_ids = self._spell_candidates(definition_zh, definition_en, row, entry_id)
        display_spell_id = spell_ids[0] if spell_ids else 0
        name_zh = self._resolve_name(definition_zh, db2['spell_names_zh'], spell_ids)
        name_en = self._resolve_name(definition_en, db2['spell_names_en'], spell_ids)
        description_zh = self._resolve_description(definition_zh, db2['spells_zh'], spell_ids)
        description_en = self._resolve_description(definition_en, db2['spells_en'], spell_ids)
        icon = self._resolve_db2_icon(definition_zh, definition_en, spell_ids, db2)

        parents = []
        for parent_trait_node_id in db2['edges'].get(trait_node_id, []):
            parent_entries = sorted(db2['node_entries'].get(parent_trait_node_id) or [])
            if parent_entries:
                parents.append(parent_entries[0])

        return {
            'talent_id': trait_node_id or row.talent_id,
            'display_spell_id': display_spell_id or row.display_spell_id,
            'name': (name_en or row.name or '')[:255],
            'name_zh': (name_zh or row.name_zh or '')[:255],
            'icon': icon or row.icon or '',
            'row': self._coerce_int(trait_node.get('PosY')) or row.row,
            'column': self._coerce_int(trait_node.get('PosX')) or row.column,
            'max_points': self._coerce_int(entry.get('MaxRanks')) or row.max_points or 1,
            'parents_json': sorted(set(parents)),
            'description': description_en or row.description or '',
            'description_zh': description_zh or row.description_zh or '',
            'db2_subtree_id': self._coerce_int(trait_node.get('TraitSubTreeID')),
            'db2_tree_id': self._coerce_int(trait_node.get('TraitTreeID')) or row.db2_tree_id,
            'flags': self._coerce_int(trait_node.get('Flags')),
        }

    def _spell_candidates(self, definition_zh, definition_en, row, entry_id):
        candidates = []
        for definition in (definition_zh, definition_en):
            for field in ('VisibleSpellID', 'SpellID', 'OverridesSpellID'):
                value = self._coerce_int(definition.get(field))
                if value and value not in candidates:
                    candidates.append(value)
        for value in (row.display_spell_id, row.spell_id, entry_id):
            value = self._coerce_int(value)
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    def _resolve_name(self, definition, spell_names, spell_ids):
        value = (definition.get('OverrideName_lang') or '').strip()
        if value:
            return value
        for spell_id in spell_ids:
            value = (spell_names.get(spell_id) or {}).get('Name_lang', '').strip()
            if value:
                return value
        return ''

    def _resolve_description(self, definition, spells, spell_ids):
        value = (definition.get('OverrideDescription_lang') or '').strip()
        if value:
            return self._clean_db2_description(value)
        for spell_id in spell_ids:
            value = (spells.get(spell_id) or {}).get('Description_lang', '').strip()
            if value:
                return self._clean_db2_description(value)
        return ''

    def _resolve_db2_icon(self, definition_zh, definition_en, spell_ids, db2):
        for definition in (definition_zh, definition_en):
            file_data_id = self._coerce_int(definition.get('OverrideIcon'))
            icon = db2['fdid_to_icon'].get(file_data_id, '')
            if icon:
                return icon
        for spell_id in spell_ids:
            icon = db2['spell_to_icon'].get(spell_id, '')
            if icon:
                return icon
        return ''

    def _is_trusted_wowhead_icon(self, icon_data, row):
        # Some Wowhead spell pages return short/interstitial pages whose generic
        # script snippets contain unrelated icons. Only use HTML icons when the
        # parsed page title matches this node's current name.
        names = {name.strip().lower() for name in (row.name, row.name_zh) if name}
        titles = {name.strip().lower() for name in (icon_data.get('name_en'), icon_data.get('name_zh')) if name}
        titles.discard('spells')
        titles.discard('技能')
        return bool(names and titles and names.intersection(titles))

    def _collect_icon_spell_ids(self, rows, expected, refresh_all=False):
        spell_ids = set()
        for row in rows:
            values = expected[row.id]
            spell_id = self._coerce_int(values.get('display_spell_id') or row.display_spell_id or row.spell_id)
            if not spell_id:
                continue
            if refresh_all or not row.icon or (values.get('name_zh') and row.name_zh != values['name_zh']) or (values.get('name') and row.name != values['name']):
                spell_ids.add(spell_id)
        return spell_ids

    def _collect_bad_description_spell_ids(self, rows):
        spell_ids = set()
        for row in rows:
            description = row.description_zh or ''
            if not description or self._has_bad_description_tokens(description):
                spell_id = self._coerce_int(row.display_spell_id or row.spell_id)
                if spell_id:
                    spell_ids.add(spell_id)
        return spell_ids

    def _fill_icon_cache(self, spell_ids, cache, cache_path, workers):
        missing = [spell_id for spell_id in sorted(spell_ids) if str(spell_id) not in cache]
        self.stdout.write(f'Wowhead icon cache: {len(cache)} cached, {len(missing)} missing')
        if not missing:
            return
        self._fill_cache(missing, cache, cache_path, workers, self._fetch_wowhead_icon)

    def _fill_desc_cache(self, spell_ids, cache, cache_path, workers):
        missing = [spell_id for spell_id in sorted(spell_ids) if str(spell_id) not in cache]
        self.stdout.write(f'Wowhead desc cache: {len(cache)} cached, {len(missing)} missing')
        if not missing:
            return
        self._fill_cache(missing, cache, cache_path, workers, self._fetch_wowhead_desc)

    def _fill_cache(self, missing, cache, cache_path, workers, fetcher):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetcher, spell_id) for spell_id in missing]
            for index, future in enumerate(as_completed(futures), 1):
                spell_id, data = future.result()
                cache[str(spell_id)] = data
                if index % 25 == 0:
                    self.stdout.write(f'  fetched {index}/{len(missing)}')
                    self._write_json(cache_path, cache)
        self._write_json(cache_path, cache)

    def _fetch_wowhead_icon(self, spell_id):
        data = {'icon': '', 'name_en': '', 'name_zh': ''}
        for locale, url in (
            ('en', f'https://www.wowhead.com/spell={spell_id}'),
            ('zh', f'https://www.wowhead.com/cn/spell={spell_id}'),
        ):
            text = self._fetch_url(url)
            icon = self._extract_icon(text)
            if icon and not data['icon']:
                data['icon'] = icon
            title = self._extract_title(text)
            if locale == 'en':
                data['name_en'] = title
            else:
                data['name_zh'] = title
        return int(spell_id), data

    def _fetch_wowhead_desc(self, spell_id):
        text = self._fetch_url(f'https://www.wowhead.com/cn/spell={spell_id}')
        return int(spell_id), {
            'description_zh': self._extract_description(text),
            'name_zh': self._extract_title(text),
        }

    def _fetch_url(self, url):
        try:
            response = requests.get(url, timeout=18, headers={'User-Agent': 'Mozilla/5.0'})
            if response.status_code == 200:
                return response.text or ''
        except requests.RequestException:
            return ''
        return ''

    def _extract_icon(self, text):
        match = re.search(r"icon:\s*'([^']+)'", text or '') or re.search(r'"icon":"([^"]+)"', text or '')
        return match.group(1).strip() if match else ''

    def _extract_title(self, text):
        match = re.search(r'<title>(.*?)</title>', text or '', re.S)
        if not match:
            return ''
        title = html.unescape(match.group(1)).split(' - ')[0].split('—')[0].strip()
        if title in ('技能', '法术'):
            return ''
        return title

    def _extract_description(self, text):
        match = re.search(r'<meta name="description" content="(.*?)"', text or '', re.S)
        if not match:
            return ''
        return self._clean_wowhead_description(match.group(1))

    def _clean_wowhead_description(self, value):
        value = html.unescape(value or '')
        value = value.replace('\u200b', '').replace('\ufeff', '')
        value = re.sub(r'\s*\.?\s*\[In the .*? category\.\]', ' ', value)
        value = re.sub(r'\s*\.?\s*\[Learn how.*?\]', ' ', value)
        value = re.sub(r'\s*\.?\s*\[[^\]]*法术\.\]', ' ', value)
        value = re.sub(r'\s*\.?\s*\[在.*?分类中。\]', ' ', value)
        return self._normalize_description(value)

    def _clean_db2_description(self, value):
        value = value or ''
        value = re.sub(r'\|C[0-9A-Fa-f]{8}', '', value)
        value = value.replace('|cffffffff', '').replace('|CFFFFFFFF', '')
        value = value.replace('|r', '').replace('|R', '')
        value = re.sub(r'\$\?[^\s。；，]*', '', value)
        return self._normalize_description(value)

    def _normalize_description(self, value):
        value = html.unescape(value or '')
        value = re.sub(r'\s+', ' ', value).strip()
        value = value.replace('。。', '。')
        return value

    def _has_bad_description_tokens(self, value):
        return any(token in (value or '') for token in BAD_DESCRIPTION_TOKENS)

    def _write_spell_snapshots(self, cache, build, now):
        for spell_id, data in cache.items():
            description = (data or {}).get('description_zh') or ''
            if not description or self._has_bad_description_tokens(description):
                continue
            WowSpellSnapshot.objects.update_or_create(
                branch='wow',
                locale='zhCN',
                spell_id=int(spell_id),
                defaults={
                    'name_zh': ((data or {}).get('name_zh') or '')[:255],
                    'description': description,
                    'snapshot_build': build,
                    'updated_at': now,
                },
            )

    def _read_csv(self, dump_dir, filename):
        path = os.path.join(dump_dir, filename)
        if not os.path.exists(path):
            return []
        with open(path, newline='') as handle:
            return list(csv.DictReader(handle))

    def _map_csv(self, dump_dir, filename):
        return {self._coerce_int(row.get('ID')): row for row in self._read_csv(dump_dir, filename) if self._coerce_int(row.get('ID'))}

    def _load_icon_cache(self, dump_dir):
        icons = {}
        for row in self._read_csv(dump_dir, 'file_data_icon_cache.csv'):
            file_data_id = self._coerce_int(row.get('FileDataID'))
            icon = (row.get('IconName') or '').strip()
            if file_data_id and icon:
                icons[file_data_id] = icon
        return icons

    def _load_json(self, path):
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))

    def _coerce_int(self, value):
        try:
            return int(str(value).strip() or 0)
        except (TypeError, ValueError):
            return 0
