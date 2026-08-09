import csv
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.models import SimcBenchmarkCandidate, SimcBenchmarkPanel, SimcBenchmarkProfile
from botend.services.simc_benchmark_tooltip_generator import (
    normalize_tooltip_text,
    parse_simc_spell_query,
    render_item_stats,
    render_spell_description,
)


BUILD_RE = re.compile(r'World of Warcraft\s+(?P<build>\d+\.\d+\.\d+\.\d+)\s+PTR')
VALUE_TOKEN_RE = re.compile(
    r'\$(?P<spell_id>\d+)?(?P<kind>[sdwut])(?P<effect_index>\d*)',
    re.IGNORECASE,
)
SPELL_DESCRIPTION_TOKEN_RE = re.compile(r'\$@spelldesc(?P<spell_id>\d+)', re.IGNORECASE)
TABLES = ('Item', 'ItemSparse', 'ItemXItemEffect', 'ItemEffect', 'Spell', 'SpellName')
ICON_NAME_RE = re.compile(r'interface/icons/(?P<name>[a-z0-9_]+)\.blp', re.IGNORECASE)


def _candidate_identity(params):
    swap = params.get('gear_swap') if isinstance(params, dict) else None
    if not isinstance(swap, dict):
        return None
    item_id = swap.get('item_id')
    item_level = swap.get('item_level') or swap.get('ilevel')
    if not isinstance(item_level, int):
        match = re.search(
            r'(?:^|,)\s*ilevel\s*=\s*(\d+)(?:\s*,|$)',
            str(swap.get('raw_value') or ''),
            re.IGNORECASE,
        )
        item_level = int(match.group(1)) if match else None
    if not isinstance(item_id, int) or item_id <= 0:
        return None
    if not isinstance(item_level, int) or item_level <= 0:
        return None
    return item_id, item_level


def _download_table(cache_dir, table, build, locale):
    path = cache_dir / f'{table}.{build}.{locale}.csv'
    if path.exists() and path.stat().st_size:
        return path
    url = f'https://wago.tools/db2/{table}/csv?build={build}&locale={locale}'
    temporary = path.with_suffix(path.suffix + '.tmp')
    request = Request(
        url,
        headers={
            'Accept': 'text/csv,*/*;q=0.8',
            'User-Agent': 'LMonitor-SimcBenchmarkTooltipGenerator/1.0',
        },
    )
    try:
        with urlopen(request, timeout=180) as response, temporary.open('wb') as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        temporary.replace(path)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise CommandError(f'下载 Wago {table} 失败：{exc}') from exc
    return path


def _filtered_rows(path, field, values):
    expected = {str(value) for value in values}
    rows = []
    with path.open(newline='', encoding='utf-8-sig') as source:
        reader = csv.DictReader(source)
        if field not in (reader.fieldnames or []):
            raise CommandError(f'{path.name} 缺少字段 {field}')
        for row in reader:
            if row.get(field) in expected:
                rows.append(row)
    return rows


def _positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _apply_tooltip_candidate_override(profile_text, slot, raw_value):
    """Mirror production override: replace first base slot before candidate section."""
    lines = []
    replaced = False
    in_candidate_section = False
    for line in str(profile_text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('###'):
            in_candidate_section = True
        current = line.partition('=')[0].strip().lower()
        if current == str(slot).strip().lower() and not replaced and not in_candidate_section:
            lines.append(f'{current}={str(raw_value)}')
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        raise ValueError(f'基准玩家块未包含可替换的装备槽位: {slot}')
    return '\n'.join(lines) + '\n'


def _normalize_simc_gear_stats(gear):
    if not isinstance(gear, dict):
        raise ValueError('SimC JSON gear missing')
    nested = gear.get('stats')
    if isinstance(nested, dict):
        return nested
    stat_keys = (
        'stragiint', 'stragi', 'strint', 'agiint', 'strength', 'agility', 'intellect', 'crit_rating',
        'haste_rating', 'mastery_rating', 'versatility_rating', 'stamina',
        'armor', 'crit', 'haste', 'mastery', 'versatility',
    )
    stats = {}
    for key in stat_keys:
        if key in gear and isinstance(gear[key], (int, float)):
            stats[key.removesuffix('_rating')] = gear[key]
    if not stats:
        raise ValueError('SimC JSON gear stats missing')
    return stats


def _parse_gear_item(report, item_id, item_level, build):
    try:
        actual_build = report['sim']['options']['dbc']['PTR']['wow_version']
    except (KeyError, TypeError) as exc:
        raise ValueError('SimC JSON report missing PTR build') from exc
    if actual_build != build:
        raise ValueError(f'SimC JSON build mismatch: expected {build}, got {actual_build}')
    sim = report.get('sim') if isinstance(report, dict) else None
    players = sim.get('players') if isinstance(sim, dict) else None
    for player in players or []:
        for gear in (player.get('gear') or {}).values():
            encoded = str(gear.get('encoded_item') or '')
            id_match = re.search(r'(?:^|,)id=(\d+)(?:,|$)', encoded)
            level_match = re.search(r'(?:^|,)ilevel=(\d+)(?:,|$)', encoded)
            actual_id = int(id_match.group(1)) if id_match else None
            actual_level = gear.get('ilevel') or (
                int(level_match.group(1)) if level_match else None
            )
            if actual_id == item_id and actual_level == item_level:
                gear = dict(gear)
                gear['stats'] = _normalize_simc_gear_stats(gear)
                return gear
    raise ValueError(f'SimC JSON missing exact item={item_id}, ilevel={item_level}')


def _is_jpeg(content):
    return len(content) >= 4 and content.startswith(b'\xff\xd8\xff') and content.endswith(b'\xff\xd9')


def _ensure_icon_file(icon_name, static_dir, source_url=None):
    icon_name = str(icon_name or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9_]+', icon_name):
        raise ValueError(f'invalid icon name: {icon_name!r}')
    path = Path(static_dir) / 'wow_icons' / 'small' / f'{icon_name}.jpg'
    if path.exists():
        content = path.read_bytes()
        if not _is_jpeg(content):
            raise ValueError(f'local icon is not JPEG: {path}')
    else:
        url = source_url or f'https://wow.zamimg.com/images/wow/icons/small/{icon_name}.jpg'
        request = Request(url, headers={'User-Agent': 'LMonitor-SimcBenchmarkTooltipGenerator/2.0'})
        with urlopen(request, timeout=60) as response:
            content = response.read(2 * 1024 * 1024)
        if not _is_jpeg(content):
            raise ValueError(f'downloaded icon is not JPEG: {url}')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.jpg.tmp')
        temporary.write_bytes(content)
        temporary.replace(path)
    return f'/static/wow_icons/small/{icon_name}.jpg'


def _resolve_icon_name(file_data_id, cache_dir):
    cache_path = Path(cache_dir) / 'icons' / f'{file_data_id}.txt'
    if cache_path.exists() and cache_path.stat().st_size:
        body = cache_path.read_text(encoding='utf-8')
    else:
        request = Request(
            f'https://wago.tools/files?search={file_data_id}',
            headers={'User-Agent': 'LMonitor-SimcBenchmarkTooltipGenerator/2.0'},
        )
        with urlopen(request, timeout=60) as response:
            body = response.read(4 * 1024 * 1024).decode('utf-8', errors='replace')
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(body, encoding='utf-8')
    normalized = html.unescape(body).replace(r'\/', '/')
    match = ICON_NAME_RE.search(normalized)
    if not match:
        raise CommandError(f'无法从 FDID {file_data_id} 解析 icon 名。')
    return match.group('name').lower()


def _query_simc_gear(binary, cache_dir, profile_text, slot, raw_value, item_id, item_level, build):
    output_path = cache_dir / 'simc' / build / f'item-{item_id}-ilevel-{item_level}.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists() or not output_path.stat().st_size:
        profile = _apply_tooltip_candidate_override(profile_text, slot, raw_value)
        with tempfile.NamedTemporaryFile('w', suffix='.simc', encoding='utf-8', delete=False) as source:
            source.write(profile)
            source_path = Path(source.name)
        try:
            completed = subprocess.run(
                [
                    str(binary), str(source_path), 'ptr=1', 'iterations=1',
                    'threads=1', 'max_time=1', f'json2={output_path}',
                    'report_details=0',
                ],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=180, check=False,
            )
        finally:
            source_path.unlink(missing_ok=True)
        if completed.returncode != 0 or not output_path.is_file():
            output_path.unlink(missing_ok=True)
            raise CommandError(f'SimC gear JSON 失败：item={item_id}, ilevel={item_level}')
    try:
        report = json.loads(output_path.read_text(encoding='utf-8'))
        return _parse_gear_item(report, item_id, item_level, build)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CommandError(str(exc)) from exc


def _build_tooltip_record(
    *, item_id, item_level, name_zh, icon_url, icon_name, icon_file_data_id,
    gear_item, rendered_effects, spell_ids, templates, unresolved_tokens,
):
    stats = gear_item.get('stats') if isinstance(gear_item, dict) else None
    if not isinstance(stats, dict):
        raise ValueError('SimC gear stats missing')
    if not stats and not gear_item.get('simc_fallback'):
        raise ValueError('SimC gear stats missing')
    stat_order = {
        key: index for index, key in enumerate((
            'stragiint', 'stragi', 'strint', 'agiint', 'strength', 'agility', 'intellect', 'crit', 'haste',
            'mastery', 'versatility', 'stamina', 'armor',
        ))
    }
    structured_stats = []
    for key, value in sorted(stats.items(), key=lambda pair: (stat_order.get(pair[0], 999), pair[0])):
        rendered = render_item_stats({key: value})
        if rendered:
            structured_stats.append({'key': key, 'value': value, 'text': rendered[0]})
    effects = []
    for value in rendered_effects or []:
        effect = normalize_tooltip_text(value)
        if effect and effect not in effects:
            effects.append(effect)
    description = '\n'.join([
        *(stat['text'] for stat in structured_stats),
        *effects,
    ])
    if not description:
        raise ValueError('tooltip has no display content')
    return {
        'item_id': item_id,
        'item_level': item_level,
        'name_zh': str(name_zh or '').strip(),
        'icon_file_data_id': icon_file_data_id,
        'icon_name': str(icon_name or '').strip(),
        'icon_url': str(icon_url or '').strip(),
        'stats': structured_stats,
        'effects': effects,
        'description_zh': description,
        'spell_ids': sorted(set(spell_ids or [])),
        'templates': list(templates or []),
        'unresolved_tokens': list(unresolved_tokens or []),
        'audit': {
            'icon_file_data_id': icon_file_data_id,
            'simc_encoded_item': str(gear_item.get('encoded_item') or ''),
            'simc_item_level': gear_item.get('ilevel'),
            'simc_fallback': bool(gear_item.get('simc_fallback')),
            'fallback_reason': gear_item.get('fallback_reason', ''),
        },
    }


def _referenced_spell_ids(text, default_spell_id):
    return {
        int(match.group('spell_id') or default_spell_id)
        for match in VALUE_TOKEN_RE.finditer(text or '')
    }


def _required_spell_ids(text, default_spell_id, spell_descriptions):
    required = {default_spell_id}
    pending = [(str(text or ''), default_spell_id)]
    visited_descriptions = set()
    while pending:
        current_text, current_spell_id = pending.pop()
        required.update(_referenced_spell_ids(current_text, current_spell_id))
        for match in SPELL_DESCRIPTION_TOKEN_RE.finditer(current_text):
            spell_id = int(match.group('spell_id'))
            required.add(spell_id)
            if spell_id in visited_descriptions:
                continue
            visited_descriptions.add(spell_id)
            description = spell_descriptions.get(spell_id)
            if description:
                pending.append((description, spell_id))
    return required


def _simc_revision(binary):
    completed = subprocess.run(
        ['git', '-C', str(binary.parent), 'rev-parse', 'HEAD'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False, timeout=30,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ''


def _query_simc(binary, cache_dir, spell_id, item_level, build):
    output_path = (
        cache_dir / 'simc' / build
        / f'spell-{spell_id}-ilevel-{item_level}.txt'
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.stat().st_size:
        output = output_path.read_text(encoding='utf-8')
    else:
        completed = subprocess.run(
            [str(binary), 'ptr=1', f'spell_query=spell.id={spell_id}@{item_level}',
             'spell_query_wrap=240'],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        output = completed.stdout
        if completed.returncode != 0:
            raise CommandError(
                f'SimC 查询失败：spell={spell_id}, ilevel={item_level}, '
                f'exit={completed.returncode}'
            )
        output_path.write_text(output, encoding='utf-8')
    match = BUILD_RE.search(output)
    if not match or match.group('build') != build:
        raise CommandError(
            f'SimC build 不匹配：期望 {build}，实际 '
            f'{match.group("build") if match else "无法识别"}'
        )
    parsed = parse_simc_spell_query(output)
    if not parsed['effects']:
        raise CommandError(f'SimC 查询无 Effect：spell={spell_id}, ilevel={item_level}')
    return parsed


class Command(BaseCommand):
    help = '为 Benchmark Panel 离线生成 item_id + ilevel 中文 tooltip 固定数据。'

    def add_arguments(self, parser):
        parser.add_argument('--panel-slug', required=True)
        parser.add_argument('--build', required=True)
        parser.add_argument('--simc-binary', required=True)
        parser.add_argument('--output', required=True)
        parser.add_argument('--cache-dir', required=True)
        parser.add_argument('--locale', default='zhCN')

    def handle(self, *args, **options):
        panel_slug = str(options['panel_slug']).strip()
        build = str(options['build']).strip()
        locale = str(options['locale']).strip()
        binary = Path(options['simc_binary']).expanduser().resolve()
        output_path = Path(options['output']).expanduser().resolve()
        cache_dir = Path(options['cache_dir']).expanduser().resolve()
        if not binary.is_file():
            raise CommandError(f'SimC binary 不存在：{binary}')
        try:
            panel = SimcBenchmarkPanel.objects.get(slug=panel_slug)
        except SimcBenchmarkPanel.DoesNotExist as exc:
            raise CommandError(f'Benchmark Panel 不存在：{panel_slug}') from exc

        candidates_by_identity = {}
        for params, existing_effect in SimcBenchmarkCandidate.objects.filter(
            panel=panel, is_enabled=True,
        ).values_list('params', 'effect'):
            identity = _candidate_identity(params)
            if identity:
                swap = params.get('gear_swap') or {}
                existing = candidates_by_identity.setdefault(
                    identity, {'swap': swap, 'existing_effect': existing_effect},
                )
                if (
                    existing['swap'].get('slot') != swap.get('slot')
                    or existing['swap'].get('raw_value') != swap.get('raw_value')
                ):
                    raise CommandError(
                        f'候选键存在不一致 gear_swap：item={identity[0]}, '
                        f'ilevel={identity[1]}'
                    )
        identities = set(candidates_by_identity)
        if not identities:
            raise CommandError('Panel 没有可生成 tooltip 的启用候选。')

        selected_profile = SimcBenchmarkProfile.objects.filter(
            panel_spec__panel=panel,
            panel_spec__is_enabled=True,
            is_enabled=True,
            profile__use_ptr=True,
        ).select_related('profile').order_by(
            'panel_spec__display_order', 'panel_spec_id', 'display_order', 'id',
        ).first()
        if selected_profile is None or not str(selected_profile.profile.player_equipment or '').strip():
            raise CommandError('Panel 没有可用于同 build 属性快照的启用 PTR Profile。')
        profile_text = selected_profile.profile.player_equipment

        cache_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            table: _download_table(cache_dir, table, build, locale)
            for table in TABLES
        }
        item_ids = {item_id for item_id, _ in identities}
        item_rows = {
            int(row['ID']): row
            for row in _filtered_rows(paths['Item'], 'ID', item_ids)
        }
        sparse_rows = {
            int(row['ID']): row
            for row in _filtered_rows(paths['ItemSparse'], 'ID', item_ids)
        }
        missing_item_facts = sorted(
            item_id for item_id in item_ids
            if item_id not in item_rows or item_id not in sparse_rows
        )
        if missing_item_facts:
            raise CommandError(f'同 build DB2 缺少 Item/ItemSparse：{missing_item_facts}')
        links = _filtered_rows(paths['ItemXItemEffect'], 'ItemID', item_ids)
        effect_ids = {
            value for row in links
            if (value := _positive_int(row.get('ItemEffectID')))
        }
        effects = {
            int(row['ID']): row
            for row in _filtered_rows(paths['ItemEffect'], 'ID', effect_ids)
        }
        item_spells = {}
        for link in links:
            item_id = _positive_int(link.get('ItemID'))
            effect_id = _positive_int(link.get('ItemEffectID'))
            spell_id = _positive_int((effects.get(effect_id) or {}).get('SpellID'))
            if item_id and spell_id:
                item_spells.setdefault(item_id, set()).add(spell_id)
        associated_spell_ids = set().union(*item_spells.values()) if item_spells else set()
        spell_rows = {
            int(row['ID']): row
            for row in _filtered_rows(paths['Spell'], 'ID', associated_spell_ids)
        }
        description_spell_ids = set(associated_spell_ids)
        while True:
            missing_spell_ids = description_spell_ids - set(spell_rows)
            if missing_spell_ids:
                spell_rows.update({
                    int(row['ID']): row
                    for row in _filtered_rows(paths['Spell'], 'ID', missing_spell_ids)
                })
            nested_spell_ids = {
                int(match.group('spell_id'))
                for spell_id in description_spell_ids
                for field in ('Description_lang', 'AuraDescription_lang')
                for match in SPELL_DESCRIPTION_TOKEN_RE.finditer(
                    str((spell_rows.get(spell_id) or {}).get(field) or '')
                )
            }
            expanded_spell_ids = description_spell_ids | nested_spell_ids
            if expanded_spell_ids == description_spell_ids:
                break
            description_spell_ids = expanded_spell_ids

        templates = {}
        for item_id, spell_ids in item_spells.items():
            entries = []
            for spell_id in sorted(spell_ids):
                row = spell_rows.get(spell_id) or {}
                field = 'Description_lang'
                text = str(row.get(field) or '').strip()
                if not text:
                    field = 'AuraDescription_lang'
                    text = str(row.get(field) or '').strip()
                if text and text not in [entry['template'] for entry in entries]:
                    entries.append({'spell_id': spell_id, 'field': field, 'template': text})
            templates[item_id] = entries

        description_templates = {}
        for spell_id in sorted(description_spell_ids):
            row = spell_rows.get(spell_id) or {}
            text = str(row.get('Description_lang') or '').strip()
            if not text:
                text = str(row.get('AuraDescription_lang') or '').strip()
            if text:
                description_templates[spell_id] = text

        revision = _simc_revision(binary)
        if not revision:
            raise CommandError('无法读取 SimC revision。')

        tooltips = []
        missing_templates = []
        unresolved_count = 0
        for item_id, item_level in sorted(identities):
            candidate_facts = candidates_by_identity[(item_id, item_level)]
            swap = candidate_facts['swap']
            slot = str(swap.get('slot') or '').strip()
            raw_value = str(swap.get('raw_value') or '').strip()
            if not slot or not raw_value:
                raise CommandError(
                    f'候选缺少 slot/raw_value：item={item_id}, ilevel={item_level}'
                )
            name_zh = str(sparse_rows[item_id].get('Display_lang') or '').strip()
            icon_file_data_id = _positive_int(item_rows[item_id].get('IconFileDataID'))
            if not name_zh or not icon_file_data_id:
                raise CommandError(
                    f'同 build DB2 展示事实不完整：item={item_id}, ilevel={item_level}'
                )
            icon_name = _resolve_icon_name(icon_file_data_id, cache_dir)
            try:
                icon_url = _ensure_icon_file(icon_name, Path(settings.BASE_DIR) / 'static')
            except (OSError, ValueError) as exc:
                raise CommandError(
                    f'图标发布校验失败：item={item_id}, fdid={icon_file_data_id}: {exc}'
                ) from exc
            simc_fallback = False
            try:
                gear_item = _query_simc_gear(
                    binary, cache_dir, profile_text, slot, raw_value,
                    item_id, item_level, build,
                )
            except CommandError as exc:
                simc_fallback = True
                gear_item = {
                    'encoded_item': raw_value,
                    'ilevel': item_level,
                    'stats': {},
                    'simc_fallback': True,
                    'fallback_reason': str(exc),
                }
            entries = templates.get(item_id) or []
            if not entries:
                if not simc_fallback:
                    missing_templates.append({'item_id': item_id, 'item_level': item_level})
                    continue
                entries = []
            needed = set()
            for entry in entries:
                needed.update(_required_spell_ids(
                    entry['template'], entry['spell_id'], description_templates,
                ))
            queries = {
                spell_id: _query_simc(
                    binary, cache_dir, spell_id, item_level, build,
                )
                for spell_id in sorted(needed)
            }
            rendered_parts = []
            unresolved = []
            for entry in entries:
                rendered, entry_unresolved = render_spell_description(
                    entry['template'], base_spell_id=entry['spell_id'],
                    spell_queries=queries,
                    spell_descriptions=description_templates,
                )
                if rendered and rendered not in rendered_parts:
                    rendered_parts.append(rendered)
                for token in entry_unresolved:
                    if token not in unresolved:
                        unresolved.append(token)
            if not rendered_parts and not simc_fallback:
                missing_templates.append({'item_id': item_id, 'item_level': item_level})
                continue
            if simc_fallback:
                fallback_note = 'SimC 未适配该物品：以下为原始属性/效果，未按目标装等缩放。'
                historical_effect = str(candidate_facts['existing_effect'] or '').strip()
                rendered_parts.insert(0, fallback_note)
                if historical_effect and historical_effect not in rendered_parts:
                    rendered_parts.append(historical_effect)
            unresolved_count += int(bool(unresolved))
            tooltips.append(_build_tooltip_record(
                item_id=item_id,
                item_level=item_level,
                name_zh=name_zh,
                icon_file_data_id=icon_file_data_id,
                icon_name=icon_name,
                icon_url=icon_url,
                gear_item=gear_item,
                rendered_effects=rendered_parts,
                spell_ids=needed,
                templates=entries,
                unresolved_tokens=unresolved,
            ))

        payload = {
            'schema_version': 2,
            'source': {
                'wago_build': build,
                'wago_locale': locale,
                'simc_build': build,
                'simc_revision': revision,
                'simc_profile_id': selected_profile.profile_id,
            },
            'panel': {'slug': panel.slug, 'id': panel.id},
            'coverage': {
                'candidate_keys': len(identities),
                'generated': len(tooltips),
                'missing_templates': missing_templates,
                'with_unresolved_tokens': unresolved_count,
            },
            'tooltips': tooltips,
        }
        if len(tooltips) + len(missing_templates) != len(identities):
            raise CommandError('生成覆盖统计不闭合，未写入输出文件。')
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + '.tmp')
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        temporary.replace(output_path)
        self.stdout.write(self.style.SUCCESS(
            f'generated {len(tooltips)}/{len(identities)} candidate tooltips; '
            f'missing templates {len(missing_templates)}; unresolved {unresolved_count}; '
            f'output {output_path}'
        ))
