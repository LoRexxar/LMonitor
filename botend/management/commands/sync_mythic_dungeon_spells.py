# -*- coding: utf-8 -*-
"""从明确的 Wago DB2 build 补全 MDT 怪物技能公共资料。"""

from __future__ import annotations

import csv
import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import (
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonSpell,
    WowSpellEffectSnapshot,
    WowSpellSnapshot,
    WowSpellSnapshotState,
)
from botend.wow.spell_text import SpellTextResolver


REFERENCE_RE = re.compile(
    r'\$(?:@(?:spellname|spelldesc|spelltooltip|spellaura|spellicon))?(\d+)',
    re.IGNORECASE,
)
ICON_PATH_RE = re.compile(
    r'filename&quot;:&quot;([^&]+[.]blp)&quot;',
    re.IGNORECASE,
)
TABLE_DOWNLOADS = (
    ('SpellName_enUS.csv', 'SpellName', 'enUS'),
    ('SpellName_zhCN.csv', 'SpellName', 'zhCN'),
    ('Spell_enUS.csv', 'Spell', 'enUS'),
    ('Spell_zhCN.csv', 'Spell', 'zhCN'),
    ('SpellMisc.csv', 'SpellMisc', 'zhCN'),
    ('SpellEffect.csv', 'SpellEffect', 'zhCN'),
    ('SpellDuration.csv', 'SpellDuration', 'zhCN'),
    ('SpellRadius.csv', 'SpellRadius', 'zhCN'),
)


class Command(BaseCommand):
    help = '按明确 build 从 Wago DB2 补全 MDT 技能名称、说明、数值变量和图标'

    def add_arguments(self, parser):
        parser.add_argument('--version-key', default='', help='MDT 数据版本；默认使用当前生效版本')
        parser.add_argument('--build', required=True, help='明确的 WoW build，例如 12.1.0.68914')
        parser.add_argument('--branch', default='wowt', help='数据产品/分支，例如 wowt、wow、wowxptr')
        parser.add_argument(
            '--dump-root',
            default='.cache/wago_db2_dumps',
            help='DB2 缓存根目录，实际写入 <root>/<branch>-<build>',
        )
        parser.add_argument('--refresh', action='store_true', help='重新下载已有 CSV')
        parser.add_argument('--skip-icons', action='store_true', help='不查询 FileDataID 对应图标名')
        parser.add_argument(
            '--listfile-url',
            default='',
            help='固定 wow-listfile CSV 下载地址；优先本地批量解析图标名',
        )
        parser.add_argument('--icon-workers', type=int, default=8, help='图标名查询并发数')
        parser.add_argument('--icon-delay', type=float, default=0.03, help='每个图标查询前的延迟秒数')
        parser.add_argument(
            '--wowhead-tooltips',
            action='store_true',
            help='抓取 Wowhead 服务端渲染的简中 tooltip，补足 DB2 数值变量',
        )
        parser.add_argument(
            '--tooltip-only',
            action='store_true',
            help='仅同步 Wowhead 简中 tooltip，不重新扫描 DB2 CSV',
        )
        parser.add_argument(
            '--wowhead-locale',
            type=int,
            default=4,
            help='Wowhead 语言编号；简体中文为 4',
        )
        parser.add_argument('--tooltip-workers', type=int, default=6, help='Wowhead tooltip 查询并发数')
        parser.add_argument('--tooltip-delay', type=float, default=0.05, help='每个 tooltip 查询前延迟秒数')
        parser.add_argument(
            '--min-name-coverage',
            type=float,
            default=0.8,
            help='名称最低覆盖率，低于该值拒绝写库',
        )
        parser.add_argument('--dry-run', action='store_true', help='下载并统计，但不写数据库')

    def handle(self, *args, **options):
        build = str(options.get('build') or '').strip()
        branch = str(options.get('branch') or '').strip()
        if not build or build.lower() == 'latest':
            raise CommandError('--build 必须是明确版本，禁止使用 latest')
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+', build):
            raise CommandError(f'build 格式不正确: {build}')
        if not re.fullmatch(r'[a-z0-9_-]+', branch):
            raise CommandError(f'branch 格式不正确: {branch}')

        version = self._resolve_version(str(options.get('version_key') or '').strip())
        spell_ids = set(
            MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                is_active=True,
            ).values_list('spell_id', flat=True)
        )
        if not spell_ids:
            raise CommandError(f'数据版本 {version.key} 没有可补全的怪物技能')

        dump_root = Path(str(options.get('dump_root') or '.cache/wago_db2_dumps'))
        if not dump_root.is_absolute():
            dump_root = Path(settings.BASE_DIR) / dump_root
        dump_dir = dump_root / f'{branch}-{build}'
        dump_dir.mkdir(parents=True, exist_ok=True)

        self.stdout.write(
            f'目标版本: {version.key}, branch={branch}, build={build}, '
            f'unique_spells={len(spell_ids)}'
        )
        tooltip_cache_path = dump_dir / 'wowhead_tooltips_zhCN.csv'
        tooltip_locale = max(0, int(options.get('wowhead_locale') or 4))
        if options.get('tooltip_only'):
            wowhead_tooltips = self._load_tooltip_cache(tooltip_cache_path)
            wowhead_tooltips = self._resolve_wowhead_tooltips(
                spell_ids,
                wowhead_tooltips,
                workers=max(1, int(options.get('tooltip_workers') or 1)),
                delay=max(0.0, float(options.get('tooltip_delay') or 0.0)),
                locale=tooltip_locale,
                cache_path=tooltip_cache_path,
            )
            self._write_tooltip_cache(tooltip_cache_path, wowhead_tooltips)
            if options.get('dry_run'):
                self.stdout.write(self.style.SUCCESS('tooltip-only dry-run 完成，未写数据库'))
                return
            written = self._write_tooltips_only(
                version=version,
                branch=branch,
                build=build,
                spell_ids=spell_ids,
                wowhead_tooltips=wowhead_tooltips,
                locale=tooltip_locale,
            )
            self.stdout.write(self.style.SUCCESS(
                f"Tooltip 同步完成: fetched={written['fetched']}/{len(spell_ids)}, "
                f"updated={written['updated']}, build={build}"
            ))
            return

        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        for filename, table, locale in TABLE_DOWNLOADS:
            self._download_csv(
                session,
                table=table,
                locale=locale,
                build=build,
                target=dump_dir / filename,
                refresh=bool(options.get('refresh')),
            )

        target_rows = self._load_spell_rows(dump_dir, spell_ids)
        referenced_ids = self._collect_references(target_rows)
        all_ids = set(spell_ids) | referenced_ids
        if referenced_ids:
            reference_rows = self._load_spell_rows(dump_dir, referenced_ids)
            nested_ids = self._collect_references(reference_rows) - all_ids
            all_ids |= nested_ids
        rows = self._load_spell_rows(dump_dir, all_ids)
        misc = self._load_misc(dump_dir / 'SpellMisc.csv', all_ids)
        effects = self._load_effects(dump_dir / 'SpellEffect.csv', all_ids)
        self._write_misc_index(dump_dir / 'spell_misc_index.csv', misc)

        icon_ids = {
            int(row.get('SpellIconFileDataID') or 0)
            for spell_id, row in misc.items()
            if spell_id in spell_ids and int(row.get('SpellIconFileDataID') or 0) > 0
        }
        icon_cache_path = dump_dir / 'file_data_icon_cache.csv'
        icon_names = self._load_icon_cache(icon_cache_path)
        previous_spell_metadata = (
            version.metadata.get('spell_snapshot', {})
            if isinstance(version.metadata, dict)
            and isinstance(version.metadata.get('spell_snapshot'), dict)
            else {}
        )
        listfile_url = str(
            options.get('listfile_url')
            or previous_spell_metadata.get('listfile_url')
            or ''
        ).strip()
        if listfile_url and icon_ids:
            listfile_path = dump_dir / 'verified-listfile.csv'
            self._download_file(
                session,
                url=listfile_url,
                target=listfile_path,
                refresh=bool(options.get('refresh')),
                expected_content_types=('text/csv', 'application/octet-stream'),
            )
            icon_names.update(
                self._load_icon_names_from_listfile(listfile_path, icon_ids),
            )
        if not options.get('skip_icons') and icon_ids:
            icon_names = self._resolve_icon_names(
                icon_ids,
                icon_names,
                workers=max(1, int(options.get('icon_workers') or 1)),
                delay=max(0.0, float(options.get('icon_delay') or 0.0)),
            )
            self._write_icon_cache(icon_cache_path, icon_names)

        wowhead_tooltips = self._load_tooltip_cache(tooltip_cache_path)
        if options.get('wowhead_tooltips'):
            wowhead_tooltips = self._resolve_wowhead_tooltips(
                spell_ids,
                wowhead_tooltips,
                workers=max(1, int(options.get('tooltip_workers') or 1)),
                delay=max(0.0, float(options.get('tooltip_delay') or 0.0)),
                locale=tooltip_locale,
                cache_path=tooltip_cache_path,
            )
            self._write_tooltip_cache(tooltip_cache_path, wowhead_tooltips)

        coverage = self._coverage(
            spell_ids,
            rows,
            misc,
            icon_names,
            wowhead_tooltips,
        )
        self.stdout.write(
            '覆盖率: '
            f"name_zh={coverage['name_zh']}/{coverage['total']} "
            f"name_en={coverage['name']}/{coverage['total']} "
            f"raw_text={coverage['raw_text']}/{coverage['total']} "
            f"icon_id={coverage['icon_id']}/{coverage['total']} "
            f"icon_name={coverage['icon_name']}/{coverage['total']} "
            f"rendered_tooltip_zh={coverage['rendered_tooltip_zh']}/{coverage['total']}"
        )
        ratio = coverage['name_zh'] / max(1, coverage['total'])
        minimum = max(0.0, min(1.0, float(options.get('min_name_coverage') or 0)))
        if ratio < minimum:
            raise CommandError(
                f'中文名称覆盖率 {ratio:.1%} 低于最低要求 {minimum:.1%}，拒绝写库'
            )
        if options.get('dry_run'):
            self.stdout.write(self.style.SUCCESS('dry-run 完成，未写数据库'))
            return

        written = self._write_snapshots(
            version=version,
            branch=branch,
            build=build,
            dump_dir=dump_dir,
            spell_ids=spell_ids,
            rows=rows,
            misc=misc,
            effects=effects,
            icon_names=icon_names,
            coverage=coverage,
            listfile_url=listfile_url,
            wowhead_tooltips=wowhead_tooltips,
        )
        self.stdout.write(self.style.SUCCESS(
            f"技能资料同步完成: spells={written['spells']}, "
            f"ability_links={written['links']}, effects={written['effects']}, "
            f"build={build}"
        ))

    @staticmethod
    def _resolve_version(version_key):
        queryset = MythicDungeonDataVersion.objects.all()
        version = queryset.filter(key=version_key).first() if version_key else None
        if version_key and not version:
            raise CommandError(f'找不到 MDT 数据版本: {version_key}')
        if not version:
            version = queryset.filter(is_active=True).order_by('-imported_at').first()
        if not version:
            raise CommandError('找不到目标 MDT 数据版本')
        return version

    def _download_csv(self, session, *, table, locale, build, target, refresh):
        if target.is_file() and target.stat().st_size > 0 and not refresh:
            self.stdout.write(f'使用缓存: {target.name} ({target.stat().st_size} bytes)')
            return
        url = f'https://wago.tools/db2/{table}/csv?build={build}&locale={locale}'
        self._download_file(
            session,
            url=url,
            target=target,
            refresh=refresh,
            expected_content_types=('text/csv',),
        )

    def _download_file(
        self,
        session,
        *,
        url,
        target,
        refresh,
        expected_content_types,
    ):
        if target.is_file() and target.stat().st_size > 0 and not refresh:
            self.stdout.write(f'使用缓存: {target.name} ({target.stat().st_size} bytes)')
            return
        temporary = target.with_suffix(target.suffix + '.part')
        last_error = None
        for attempt in range(4):
            try:
                with session.get(url, timeout=180, stream=True) as response:
                    response.raise_for_status()
                    content_type = str(response.headers.get('Content-Type') or '').lower()
                    if not any(value in content_type for value in expected_content_types):
                        raise RuntimeError(
                            f'响应类型不正确: {content_type or "unknown"}',
                        )
                    with temporary.open('wb') as output:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                output.write(chunk)
                if temporary.stat().st_size <= 0:
                    raise RuntimeError('下载结果为空')
                temporary.replace(target)
                self.stdout.write(f'下载完成: {target.name} ({target.stat().st_size} bytes)')
                return
            except Exception as exc:
                last_error = exc
                if temporary.exists():
                    temporary.unlink()
                if attempt < 3:
                    time.sleep(2 ** attempt)
        raise CommandError(f'下载 {target.name} 失败: {last_error}')

    @staticmethod
    def _load_name_rows(path, ids):
        result = {}
        with path.open(encoding='utf-8-sig', newline='') as source:
            for row in csv.DictReader(source):
                spell_id = _to_int(row.get('ID'))
                if spell_id in ids:
                    result[spell_id] = str(row.get('Name_lang') or '').strip()
        return result

    @staticmethod
    def _load_text_rows(path, ids):
        result = {}
        with path.open(encoding='utf-8-sig', newline='') as source:
            for row in csv.DictReader(source):
                spell_id = _to_int(row.get('ID'))
                if spell_id in ids:
                    result[spell_id] = {
                        'description': str(row.get('Description_lang') or '').strip(),
                        'aura_description': str(row.get('AuraDescription_lang') or '').strip(),
                    }
        return result

    def _load_spell_rows(self, dump_dir, ids):
        ids = set(ids)
        names_en = self._load_name_rows(dump_dir / 'SpellName_enUS.csv', ids)
        names_zh = self._load_name_rows(dump_dir / 'SpellName_zhCN.csv', ids)
        texts_en = self._load_text_rows(dump_dir / 'Spell_enUS.csv', ids)
        texts_zh = self._load_text_rows(dump_dir / 'Spell_zhCN.csv', ids)
        return {
            spell_id: {
                'name': names_en.get(spell_id, ''),
                'name_zh': names_zh.get(spell_id, ''),
                'description': texts_en.get(spell_id, {}).get('description', ''),
                'description_zh': texts_zh.get(spell_id, {}).get('description', ''),
                'aura_description': texts_en.get(spell_id, {}).get('aura_description', ''),
                'aura_description_zh': texts_zh.get(spell_id, {}).get('aura_description', ''),
            }
            for spell_id in ids
        }

    @staticmethod
    def _collect_references(rows):
        result = set()
        for row in rows.values():
            for field in (
                'description',
                'description_zh',
                'aura_description',
                'aura_description_zh',
            ):
                for match in REFERENCE_RE.finditer(str(row.get(field) or '')):
                    result.add(_to_int(match.group(1)))
        return {spell_id for spell_id in result if spell_id > 0}

    @staticmethod
    def _load_misc(path, ids):
        result = {}
        with path.open(encoding='utf-8-sig', newline='') as source:
            for row in csv.DictReader(source):
                spell_id = _to_int(row.get('SpellID'))
                if spell_id not in ids:
                    continue
                current = result.get(spell_id)
                if current is None or (
                    _to_int(current.get('DifficultyID')) != 0
                    and _to_int(row.get('DifficultyID')) == 0
                ):
                    result[spell_id] = row
        return result

    @staticmethod
    def _load_effects(path, ids):
        result = {}
        with path.open(encoding='utf-8-sig', newline='') as source:
            for row in csv.DictReader(source):
                spell_id = _to_int(row.get('SpellID'))
                if spell_id not in ids:
                    continue
                difficulty = _to_int(row.get('DifficultyID'))
                index = _to_int(row.get('EffectIndex'))
                key = (spell_id, index)
                current = result.get(key)
                if current is None or (
                    _to_int(current.get('DifficultyID')) != 0 and difficulty == 0
                ):
                    result[key] = row
        return result

    @staticmethod
    def _write_misc_index(path, misc):
        with path.open('w', encoding='utf-8', newline='') as output:
            writer = csv.DictWriter(
                output,
                fieldnames=['SpellID', 'DurationIndex', 'RangeIndex'],
            )
            writer.writeheader()
            for spell_id, row in sorted(misc.items()):
                writer.writerow({
                    'SpellID': spell_id,
                    'DurationIndex': _to_int(row.get('DurationIndex')),
                    'RangeIndex': _to_int(row.get('RangeIndex')),
                })

    @staticmethod
    def _load_icon_cache(path):
        result = {}
        if not path.is_file():
            return result
        with path.open(encoding='utf-8-sig', newline='') as source:
            for row in csv.DictReader(source):
                file_data_id = _to_int(row.get('FileDataID'))
                if file_data_id:
                    result[file_data_id] = str(row.get('IconName') or '').strip()
        return result

    @staticmethod
    def _write_icon_cache(path, cache):
        with path.open('w', encoding='utf-8', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=['FileDataID', 'IconName'])
            writer.writeheader()
            for file_data_id, icon_name in sorted(cache.items()):
                writer.writerow({'FileDataID': file_data_id, 'IconName': icon_name})

    @staticmethod
    def _load_icon_names_from_listfile(path, icon_ids):
        result = {}
        remaining = set(icon_ids)
        with path.open(encoding='utf-8-sig', errors='replace') as source:
            for line in source:
                raw_id, separator, raw_path = line.partition(';')
                if not separator:
                    continue
                file_data_id = _to_int(raw_id)
                if file_data_id not in remaining:
                    continue
                client_path = raw_path.strip().replace('\\', '/').lower()
                if '/icons/' not in client_path or not client_path.endswith('.blp'):
                    continue
                result[file_data_id] = os.path.basename(client_path).removesuffix('.blp')
                remaining.discard(file_data_id)
                if not remaining:
                    break
        return result

    def _resolve_icon_names(self, icon_ids, cache, *, workers, delay):
        missing = sorted(file_data_id for file_data_id in icon_ids if file_data_id not in cache)
        if not missing:
            self.stdout.write(f'图标缓存命中: {len(icon_ids)}/{len(icon_ids)}')
            return cache
        self.stdout.write(
            f'查询图标名称: total={len(icon_ids)}, cached={len(icon_ids) - len(missing)}, '
            f'missing={len(missing)}, workers={workers}'
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._fetch_icon_name, file_data_id, delay): file_data_id
                for file_data_id in missing
            }
            for index, future in enumerate(as_completed(futures), start=1):
                file_data_id = futures[future]
                try:
                    cache[file_data_id] = future.result()
                except Exception:
                    cache[file_data_id] = ''
                if index % 100 == 0 or index == len(futures):
                    found = sum(1 for value in cache.values() if value)
                    self.stdout.write(
                        f'图标进度: {index}/{len(futures)}, found={found}'
                    )
        return cache

    @staticmethod
    def _load_tooltip_cache(path):
        result = {}
        if not path.is_file():
            return result
        with path.open(encoding='utf-8-sig', newline='') as source:
            for row in csv.DictReader(source):
                spell_id = _to_int(row.get('SpellID'))
                if spell_id:
                    result[spell_id] = str(row.get('DescriptionZh') or '').strip()
        return result

    @staticmethod
    def _write_tooltip_cache(path, cache):
        with path.open('w', encoding='utf-8', newline='') as output:
            writer = csv.DictWriter(
                output,
                fieldnames=['SpellID', 'DescriptionZh'],
            )
            writer.writeheader()
            for spell_id, description in sorted(cache.items()):
                writer.writerow({
                    'SpellID': spell_id,
                    'DescriptionZh': description,
                })

    def _resolve_wowhead_tooltips(
        self,
        spell_ids,
        cache,
        *,
        workers,
        delay,
        locale,
        cache_path=None,
    ):
        missing = sorted(
            spell_id
            for spell_id in spell_ids
            if self._tooltip_needs_retry(cache.get(spell_id))
        )
        if not missing:
            found = sum(bool(cache.get(spell_id)) for spell_id in spell_ids)
            self.stdout.write(f'Wowhead tooltip 缓存命中: {found}/{len(spell_ids)}')
            return cache
        self.stdout.write(
            f'查询 Wowhead 简中 tooltip: total={len(spell_ids)}, '
            f'cached={len(spell_ids) - len(missing)}, missing={len(missing)}, '
            f'workers={workers}'
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._fetch_wowhead_tooltip,
                    spell_id,
                    delay,
                    locale,
                ): spell_id
                for spell_id in missing
            }
            for index, future in enumerate(as_completed(futures), start=1):
                spell_id = futures[future]
                try:
                    description = future.result()
                    if description is not None:
                        cache[spell_id] = description
                except Exception:
                    pass
                if index % 50 == 0 or index == len(futures):
                    if cache_path is not None:
                        self._write_tooltip_cache(cache_path, cache)
                    found = sum(bool(value) for value in cache.values())
                    self.stdout.write(
                        f'Wowhead tooltip 进度: {index}/{len(futures)}, found={found}'
                    )
        return cache

    @staticmethod
    def _tooltip_needs_retry(description):
        text = str(description or '').strip()
        return not text or '$' in text

    @staticmethod
    def _fetch_wowhead_tooltip(
        spell_id,
        delay,
        locale,
        *,
        depth=0,
        seen=None,
    ):
        seen = set(seen or ())
        if spell_id in seen or depth > 2:
            return ''
        seen.add(spell_id)
        if delay:
            time.sleep(delay)
        url = f'https://nether.wowhead.com/tooltip/spell/{spell_id}'
        for attempt in range(3):
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    params={'dataEnv': 1, 'locale': locale},
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    },
                )
                if response.status_code == 404:
                    return ''
                response.raise_for_status()
                tooltip_html = str(response.json().get('tooltip') or '')
                if not tooltip_html:
                    return ''
                description = Command._description_from_tooltip_html(tooltip_html)
                if description and '$spelldesc' not in description.lower():
                    return description
                for referenced_id in Command._referenced_spell_ids_from_tooltip(
                    tooltip_html,
                    spell_id,
                ):
                    referenced = Command._fetch_wowhead_tooltip(
                        referenced_id,
                        0,
                        locale,
                        depth=depth + 1,
                        seen=seen,
                    )
                    if referenced:
                        return referenced
                return re.sub(
                    r'^\$spelldesc(?=\D)',
                    '',
                    description,
                    flags=re.IGNORECASE,
                ).strip()
            except Exception:
                if attempt < 2:
                    time.sleep(2 ** attempt)
        return None

    @staticmethod
    def _description_from_tooltip_html(tooltip_html):
        description_match = re.search(
            r'<div\s+class="q">(.*?)</div>',
            str(tooltip_html or ''),
            re.IGNORECASE | re.DOTALL,
        )
        if not description_match:
            return ''
        text = re.sub(
            r'<br\s*/?>',
            '\n',
            description_match.group(1),
            flags=re.IGNORECASE,
        )
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text).strip()
        return re.sub(r'(?<=\d)\$', '%', text)

    @staticmethod
    def _referenced_spell_ids_from_tooltip(tooltip_html, source_spell_id):
        text = html.unescape(str(tooltip_html or ''))
        result = []
        for pattern in (
            r'/[a-z]{2}/spell=(\d+)',
            r'\$spelldesc(\d+)',
        ):
            for match in re.finditer(pattern, text, re.IGNORECASE):
                spell_id = _to_int(match.group(1))
                if spell_id and spell_id != source_spell_id and spell_id not in result:
                    result.append(spell_id)
        return result

    @transaction.atomic
    def _write_tooltips_only(
        self,
        *,
        version,
        branch,
        build,
        spell_ids,
        wowhead_tooltips,
        locale,
    ):
        now = timezone.now()
        records = list(MythicDungeonSpell.objects.filter(
            data_version=version,
            spell_id__in=spell_ids,
        ))
        updated = []
        for record in records:
            description = wowhead_tooltips.get(record.spell_id)
            if not description:
                continue
            metadata = dict(record.metadata or {})
            metadata['wowhead_tooltip_url'] = (
                f'https://www.wowhead.com/cn/spell={record.spell_id}'
            )
            metadata['wowhead_tooltip_source'] = (
                f'https://nether.wowhead.com/tooltip/spell/{record.spell_id}'
            )
            metadata['wowhead_locale'] = locale
            record.description_zh = description
            record.source_branch = branch
            record.snapshot_build = build
            record.metadata = metadata
            record.updated_at = now
            updated.append(record)
        if updated:
            MythicDungeonSpell.objects.bulk_update(
                updated,
                [
                    'description_zh',
                    'source_branch',
                    'snapshot_build',
                    'metadata',
                    'updated_at',
                ],
                batch_size=500,
            )

        version_metadata = dict(version.metadata or {})
        spell_snapshot = dict(version_metadata.get('spell_snapshot') or {})
        coverage = dict(spell_snapshot.get('coverage') or {})
        coverage['total'] = len(spell_ids)
        coverage['rendered_tooltip_zh'] = sum(
            bool(wowhead_tooltips.get(spell_id))
            for spell_id in spell_ids
        )
        spell_snapshot.update({
            'source_branch': branch,
            'snapshot_build': build,
            'wowhead_tooltips': True,
            'wowhead_locale': locale,
            'coverage': coverage,
            'synced_at': now.isoformat(),
        })
        version_metadata['spell_snapshot'] = spell_snapshot
        version.metadata = version_metadata
        version.save(update_fields=['metadata', 'updated_at'])
        return {
            'fetched': coverage['rendered_tooltip_zh'],
            'updated': len(updated),
        }

    @staticmethod
    def _fetch_icon_name(file_data_id, delay):
        if delay:
            time.sleep(delay)
        url = f'https://wago.tools/files?search={file_data_id}'
        for attempt in range(3):
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    headers={'User-Agent': 'Mozilla/5.0'},
                )
                response.raise_for_status()
                for raw in ICON_PATH_RE.findall(response.text or ''):
                    path = html.unescape(raw).replace('\\/', '/').lower()
                    if '/icons/' not in path:
                        continue
                    return os.path.basename(path).removesuffix('.blp')
                return ''
            except Exception:
                if attempt < 2:
                    time.sleep(2 ** attempt)
        return ''

    @staticmethod
    def _coverage(spell_ids, rows, misc, icon_names, wowhead_tooltips):
        return {
            'total': len(spell_ids),
            'name': sum(bool(rows.get(spell_id, {}).get('name')) for spell_id in spell_ids),
            'name_zh': sum(bool(rows.get(spell_id, {}).get('name_zh')) for spell_id in spell_ids),
            'raw_text': sum(
                bool(
                    rows.get(spell_id, {}).get('description_zh')
                    or rows.get(spell_id, {}).get('aura_description_zh')
                )
                for spell_id in spell_ids
            ),
            'icon_id': sum(
                _to_int(misc.get(spell_id, {}).get('SpellIconFileDataID')) > 0
                for spell_id in spell_ids
            ),
            'icon_name': sum(
                bool(icon_names.get(
                    _to_int(misc.get(spell_id, {}).get('SpellIconFileDataID')),
                ))
                for spell_id in spell_ids
            ),
            'rendered_tooltip_zh': sum(
                bool(wowhead_tooltips.get(spell_id))
                for spell_id in spell_ids
            ),
        }

    @transaction.atomic
    def _write_snapshots(
        self,
        *,
        version,
        branch,
        build,
        dump_dir,
        spell_ids,
        rows,
        misc,
        effects,
        icon_names,
        coverage,
        listfile_url,
        wowhead_tooltips,
    ):
        now = timezone.now()
        all_ids = set(rows)
        for locale in ('enUS', 'zhCN'):
            for spell_id in all_ids:
                row = rows.get(spell_id, {})
                is_zh = locale == 'zhCN'
                WowSpellSnapshot.objects.update_or_create(
                    branch=branch,
                    locale=locale,
                    spell_id=spell_id,
                    defaults={
                        'name': row.get('name', ''),
                        'name_zh': row.get('name_zh', '') if is_zh else '',
                        'description': row.get(
                            'description_zh' if is_zh else 'description',
                            '',
                        ),
                        'aura_description': row.get(
                            'aura_description_zh' if is_zh else 'aura_description',
                            '',
                        ),
                        'snapshot_build': build,
                        'updated_at': now,
                    },
                )
            WowSpellSnapshotState.objects.update_or_create(
                branch=branch,
                locale=locale,
                defaults={'snapshot_build': build},
            )

        effect_spell_ids = sorted({spell_id for spell_id, _index in effects})
        for locale in ('enUS', 'zhCN'):
            WowSpellEffectSnapshot.objects.filter(
                branch=branch,
                locale=locale,
                spell_id__in=effect_spell_ids,
            ).delete()
            objects = []
            for (spell_id, index), row in effects.items():
                objects.append(WowSpellEffectSnapshot(
                    branch=branch,
                    locale=locale,
                    spell_id=spell_id,
                    effect_index=index,
                    effect=_to_int_or_none(row.get('Effect')),
                    effect_aura=_to_int_or_none(row.get('EffectAura')),
                    base_points=str(
                        row.get('EffectBasePointsF')
                        if row.get('EffectBasePointsF') not in (None, '')
                        else row.get('EffectBasePoints') or ''
                    ),
                    coefficient=str(
                        row.get('EffectBonusCoefficient')
                        or row.get('Coefficient')
                        or ''
                    ),
                    pvp_multiplier=str(row.get('PvpMultiplier') or ''),
                    snapshot_build=build,
                    updated_at=now,
                ))
            WowSpellEffectSnapshot.objects.bulk_create(objects, batch_size=1000)

        resolver_en = SpellTextResolver(
            locale='enUS',
            branch=branch,
            dump_dir=dump_dir,
        )
        resolver_zh = SpellTextResolver(
            locale='zhCN',
            branch=branch,
            dump_dir=dump_dir,
        )
        spell_records = {}
        for spell_id in sorted(spell_ids):
            row = rows.get(spell_id, {})
            misc_row = misc.get(spell_id, {})
            file_data_id = _to_int(misc_row.get('SpellIconFileDataID')) or None
            icon_name = icon_names.get(file_data_id, '') if file_data_id else ''
            icon_url = (
                f'https://wow.zamimg.com/images/wow/icons/large/{icon_name}.jpg'
                if icon_name
                else ''
            )
            description = resolver_en.resolve(row.get('description'), spell_id)
            description_zh = (
                wowhead_tooltips.get(spell_id)
                or resolver_zh.resolve(row.get('description_zh'), spell_id)
            )
            aura_description = resolver_en.resolve(
                row.get('aura_description'),
                spell_id,
            )
            aura_description_zh = resolver_zh.resolve(
                row.get('aura_description_zh'),
                spell_id,
            )
            record, _created = MythicDungeonSpell.objects.update_or_create(
                data_version=version,
                spell_id=spell_id,
                defaults={
                    'source_branch': branch,
                    'source_locale': 'zhCN',
                    'snapshot_build': build,
                    'name': row.get('name', ''),
                    'name_zh': row.get('name_zh', ''),
                    'description': description,
                    'description_zh': description_zh,
                    'aura_description': aura_description,
                    'aura_description_zh': aura_description_zh,
                    'icon_file_data_id': file_data_id,
                    'icon_name': icon_name,
                    'icon_url': icon_url,
                    'is_active': True,
                    'metadata': {
                        'source': 'wago.tools DB2',
                        'source_branch': branch,
                        'snapshot_build': build,
                        'raw_description': row.get('description', ''),
                        'raw_description_zh': row.get('description_zh', ''),
                        'raw_aura_description': row.get('aura_description', ''),
                        'raw_aura_description_zh': row.get('aura_description_zh', ''),
                        'listfile_url': listfile_url,
                        'wowhead_tooltip_url': (
                            f'https://www.wowhead.com/cn/spell={spell_id}'
                            if wowhead_tooltips.get(spell_id)
                            else ''
                        ),
                        'coverage': coverage,
                    },
                },
            )
            spell_records[spell_id] = record

        links = 0
        for spell_id, record in spell_records.items():
            links += MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                spell_id=spell_id,
            ).exclude(spell_record=record).update(spell_record=record)
        version_metadata = dict(version.metadata or {})
        version_metadata['spell_snapshot'] = {
            'source': 'wago.tools DB2',
            'source_branch': branch,
            'source_locale': 'zhCN',
            'snapshot_build': build,
            'listfile_url': listfile_url,
            'wowhead_tooltips': bool(wowhead_tooltips),
            'coverage': coverage,
            'synced_at': now.isoformat(),
        }
        version.metadata = version_metadata
        version.save(update_fields=['metadata', 'updated_at'])
        return {
            'spells': len(spell_records),
            'links': links,
            'effects': len(effects) * 2,
        }


def _to_int(value):
    try:
        return int(str(value or '').strip() or '0')
    except (TypeError, ValueError):
        return 0


def _to_int_or_none(value):
    if value in (None, ''):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
