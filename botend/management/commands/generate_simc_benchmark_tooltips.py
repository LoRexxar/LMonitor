import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

from django.core.management.base import BaseCommand, CommandError

from botend.models import SimcBenchmarkCandidate, SimcBenchmarkPanel
from botend.services.simc_benchmark_tooltip_generator import (
    parse_simc_spell_query,
    render_spell_description,
)


BUILD_RE = re.compile(r'World of Warcraft\s+(?P<build>\d+\.\d+\.\d+\.\d+)\s+PTR')
VALUE_TOKEN_RE = re.compile(
    r'\$(?P<spell_id>\d+)?(?P<kind>[sdwut])(?P<effect_index>\d*)',
    re.IGNORECASE,
)
SPELL_DESCRIPTION_TOKEN_RE = re.compile(r'\$@spelldesc(?P<spell_id>\d+)', re.IGNORECASE)
TABLES = ('ItemXItemEffect', 'ItemEffect', 'Spell', 'SpellName')


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

        identities = set()
        for params in SimcBenchmarkCandidate.objects.filter(
            panel=panel, is_enabled=True,
        ).values_list('params', flat=True):
            identity = _candidate_identity(params)
            if identity:
                identities.add(identity)
        if not identities:
            raise CommandError('Panel 没有可生成 tooltip 的启用候选。')

        cache_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            table: _download_table(cache_dir, table, build, locale)
            for table in TABLES
        }
        item_ids = {item_id for item_id, _ in identities}
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
            entries = templates.get(item_id) or []
            if not entries:
                missing_templates.append({'item_id': item_id, 'item_level': item_level})
                continue
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
            if not rendered_parts:
                missing_templates.append({'item_id': item_id, 'item_level': item_level})
                continue
            unresolved_count += int(bool(unresolved))
            tooltips.append({
                'item_id': item_id,
                'item_level': item_level,
                'description_zh': '\n\n'.join(rendered_parts),
                'spell_ids': sorted(needed),
                'templates': entries,
                'unresolved_tokens': unresolved,
            })

        payload = {
            'schema_version': 1,
            'source': {
                'wago_build': build,
                'wago_locale': locale,
                'simc_build': build,
                'simc_revision': revision,
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
