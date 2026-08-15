# -*- coding: utf-8 -*-
"""从明确的 Wago DB2 build 补全 MDT 怪物技能公共资料。"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit

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
from botend.mythic_planner.icon_assets import (
    build_wowhead_icon_url,
    normalize_wowhead_icon_slug,
)
from botend.mythic_planner.spell_tooltips import (
    QUALITY_EXACT_RENDERED,
    QUALITY_MECHANIC_ONLY,
    QUALITY_RENDERED_EXTERNAL,
    SOURCE_WAGO_DB2,
    SOURCE_WOWHEAD_TOOLTIP,
    SOURCE_WOWHEAD_TOOLTIP_REFERENCE,
    build_description_metadata,
    description_quality,
    preserve_description_provenance,
    should_preserve_description_for_snapshot,
)
from botend.mythic_planner.wowhead_tooltips import description_from_tooltip_html
from botend.services.article_image_service import _get_configured_proxies
from botend.wow.spell_text import SpellTextResolver


REFERENCE_RE = re.compile(
    r'\$(?:@(?:spellname|spelldesc|spelltooltip|spellaura|spellicon))?(\d+)',
    re.IGNORECASE,
)
DESCRIPTION_REFERENCE_RE = re.compile(
    r'\$@(?P<kind>spelldesc|spelltooltip)'
    r'\$?(?P<spell_id>\d+)',
    re.IGNORECASE,
)
ICON_PATH_RE = re.compile(
    r'filename&quot;:&quot;([^&]+[.]blp)&quot;',
    re.IGNORECASE,
)
CJK_RE = re.compile(r'[\u3400-\u9fff]')
WOWHEAD_DATA_ENV_BY_BRANCH = {
    'wow': 1,
    'wowt': 2,
    'wowxptr': 10,
}
WOWHEAD_ENVIRONMENT_KEYS = {
    1: 'live',
    2: 'ptr',
    3: 'beta',
    10: 'ptr2',
}
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
        parser.add_argument(
            '--dungeon-key',
            action='append',
            default=[],
            help='只同步指定地下城，可重复传入；默认同步版本内全部地下城',
        )
        parser.add_argument(
            '--spell-id',
            action='append',
            type=int,
            default=[],
            help='只同步指定技能 ID，可重复传入；适合新版本增量补全',
        )
        parser.add_argument(
            '--build',
            default='latest',
            help='WoW build；默认自动解析所选分支的最新已处理版本，也可传入明确版本',
        )
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
            help='不重新下载 DB2；用 Wowhead 简中 tooltip 与数据库现存 Wago 原始模板重建说明',
        )
        parser.add_argument(
            '--wowhead-locale',
            type=int,
            default=4,
            help='Wowhead 语言编号；简体中文为 4',
        )
        parser.add_argument(
            '--wowhead-data-env',
            type=int,
            default=0,
            help='Wowhead 数据环境；0 表示按分支自动选择（wow=1、wowt=2、wowxptr=10）',
        )
        parser.add_argument(
            '--wowhead-difficulty-id',
            type=int,
            default=8,
            help='Wowhead Tooltip 难度编号；史诗钥石为 8',
        )
        parser.add_argument('--tooltip-workers', type=int, default=6, help='Wowhead tooltip 查询并发数')
        parser.add_argument('--tooltip-delay', type=float, default=0.05, help='每个 tooltip 查询前延迟秒数')
        parser.add_argument(
            '--min-name-coverage',
            type=float,
            default=0.8,
            help='名称最低覆盖率，低于该值拒绝写库',
        )
        parser.add_argument(
            '--min-icon-coverage',
            type=float,
            default=0.8,
            help='有效图标最低覆盖率，低于该值拒绝写库',
        )
        parser.add_argument('--dry-run', action='store_true', help='下载并统计，但不写数据库')

    def handle(self, *args, **options):
        configured_build = str(options.get('build') or '').strip() or 'latest'
        branch = str(options.get('branch') or '').strip()
        if not re.fullmatch(r'[a-z0-9_-]+', branch):
            raise CommandError(f'branch 格式不正确: {branch}')
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        build = self._resolve_wago_build(
            session,
            branch=branch,
            configured_build=configured_build,
        )
        if configured_build.lower() == 'latest':
            self.stdout.write(self.style.SUCCESS(
                f'已解析 Wago 最新版本: branch={branch}, build={build}',
            ))

        version = self._resolve_version(str(options.get('version_key') or '').strip())
        dungeon_keys = self._resolve_dungeon_keys(
            version,
            options.get('dungeon_key') or [],
        )
        ability_queryset = MythicDungeonAbility.objects.filter(
            enemy__dungeon__data_version=version,
            enemy__dungeon__is_active=True,
            enemy__is_active=True,
            is_active=True,
        )
        if dungeon_keys:
            ability_queryset = ability_queryset.filter(
                enemy__dungeon__key__in=dungeon_keys,
            )
        requested_spell_ids = {
            int(spell_id)
            for spell_id in options.get('spell_id') or []
            if int(spell_id) > 0
        }
        if requested_spell_ids:
            ability_queryset = ability_queryset.filter(
                spell_id__in=requested_spell_ids,
            )
        spell_ids = set(ability_queryset.values_list('spell_id', flat=True))
        if not spell_ids:
            raise CommandError(f'数据版本 {version.key} 没有可补全的怪物技能')
        missing_requested_spell_ids = sorted(requested_spell_ids - spell_ids)
        if missing_requested_spell_ids:
            raise CommandError(
                '数据版本不包含指定技能 ID: '
                + ', '.join(str(spell_id) for spell_id in missing_requested_spell_ids)
            )

        dump_root = Path(str(options.get('dump_root') or '.cache/wago_db2_dumps'))
        if not dump_root.is_absolute():
            dump_root = Path(settings.BASE_DIR) / dump_root
        dump_dir = dump_root / f'{branch}-{build}'
        dump_dir.mkdir(parents=True, exist_ok=True)
        tooltip_data_env = self._resolve_wowhead_data_env(
            branch,
            int(options.get('wowhead_data_env') or 0),
        )
        tooltip_difficulty_id = max(
            0,
            int(options.get('wowhead_difficulty_id') or 0),
        )
        expected_tooltip_data_env = WOWHEAD_DATA_ENV_BY_BRANCH.get(branch, 1)
        if (
            (options.get('tooltip_only') or options.get('wowhead_tooltips'))
            and tooltip_data_env != expected_tooltip_data_env
        ):
            self.stdout.write(self.style.WARNING(
                f'注意：branch={branch}默认对应Wowhead dataEnv='
                f'{expected_tooltip_data_env}，本次显式使用dataEnv='
                f'{tooltip_data_env}。这些说明会标记为外部当前环境数据，'
                '不会标记为目标build精确快照。'
            ))
        if options.get('tooltip_only') or options.get('wowhead_tooltips'):
            self.stdout.write(
                'Wowhead 请求代理: '
                + (
                    '使用项目代理配置'
                    if _get_configured_proxies()
                    else '未显式配置，遵循系统环境或直连'
                )
            )
            self.stdout.write(self.style.WARNING(
                'Wowhead Tooltip 只能锁定环境与难度，不能锁定具体 build；'
                f'本次响应会记录为 {WOWHEAD_ENVIRONMENT_KEYS[tooltip_data_env]} '
                f'环境当前值（dd={tooltip_difficulty_id}），不会标记为 {build} 精确数据。'
            ))

        self.stdout.write(
            f'目标版本: {version.key}, branch={branch}, build={build}, '
            f'dungeons={",".join(dungeon_keys) if dungeon_keys else "全部"}, '
            f'unique_spells={len(spell_ids)}'
        )
        tooltip_cache_path = (
            dump_dir
            / (
                f'wowhead_tooltips_dataenv{tooltip_data_env}'
                f'_dd{tooltip_difficulty_id}_zhCN.csv'
            )
        )
        tooltip_locale = max(0, int(options.get('wowhead_locale') or 4))
        if options.get('tooltip_only'):
            self._validate_tooltip_snapshot_context(
                version,
                spell_ids,
                branch=branch,
                build=build,
            )
            description_reference_ids = self._collect_record_description_references(
                version,
                spell_ids,
            )
            tooltip_spell_ids = set(spell_ids) | description_reference_ids
            wowhead_tooltips = (
                {}
                if options.get('refresh')
                else self._load_tooltip_cache(tooltip_cache_path)
            )
            wowhead_icon_names = (
                {}
                if options.get('refresh')
                else self._load_tooltip_icon_cache(tooltip_cache_path)
            )
            wowhead_tooltips = self._resolve_wowhead_tooltips(
                tooltip_spell_ids,
                wowhead_tooltips,
                workers=max(1, int(options.get('tooltip_workers') or 1)),
                delay=max(0.0, float(options.get('tooltip_delay') or 0.0)),
                locale=tooltip_locale,
                data_env=tooltip_data_env,
                difficulty_id=tooltip_difficulty_id,
                cache_path=tooltip_cache_path,
                wowhead_icon_names=wowhead_icon_names,
            )
            self._write_tooltip_cache(
                tooltip_cache_path,
                wowhead_tooltips,
                wowhead_icon_names,
            )
            usable_tooltips = self._usable_tooltip_count(
                spell_ids,
                wowhead_tooltips,
            )
            if not usable_tooltips:
                raise CommandError(
                    f'Wowhead {WOWHEAD_ENVIRONMENT_KEYS[tooltip_data_env]} '
                    f'环境没有返回可用的简中技能说明；未写数据库。'
                    '请执行完整 DB2 同步，以目标 build 的客户端数据为准。'
                )
            written = self._write_tooltips_only(
                version=version,
                branch=branch,
                build=build,
                spell_ids=spell_ids,
                wowhead_tooltips=wowhead_tooltips,
                locale=tooltip_locale,
                data_env=tooltip_data_env,
                difficulty_id=tooltip_difficulty_id,
                dry_run=bool(options.get('dry_run')),
            )
            self.stdout.write(self.style.SUCCESS(
                f"{'Tooltip dry-run 完成（未写数据库）' if written['dry_run'] else 'Tooltip 同步完成'}: "
                f"fetched={written['fetched']}/{len(spell_ids)}, "
                f"referenced={written['referenced']}, "
                f"mechanic_only={written['mechanic_only']}, "
                f"blank={written['blank']}, updated={written['updated']}, "
                f"build={build}"
            ))
            return

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

        wowhead_tooltips = (
            {}
            if options.get('refresh')
            else self._load_tooltip_cache(tooltip_cache_path)
        )
        wowhead_icon_names = (
            {}
            if options.get('refresh')
            else self._load_tooltip_icon_cache(tooltip_cache_path)
        )
        if options.get('wowhead_tooltips'):
            description_reference_ids = self._collect_description_references(
                rows,
            )
            wowhead_tooltips = self._resolve_wowhead_tooltips(
                set(spell_ids) | description_reference_ids,
                wowhead_tooltips,
                workers=max(1, int(options.get('tooltip_workers') or 1)),
                delay=max(0.0, float(options.get('tooltip_delay') or 0.0)),
                locale=tooltip_locale,
                data_env=tooltip_data_env,
                difficulty_id=tooltip_difficulty_id,
                cache_path=tooltip_cache_path,
                wowhead_icon_names=wowhead_icon_names,
            )
            self._write_tooltip_cache(
                tooltip_cache_path,
                wowhead_tooltips,
                wowhead_icon_names,
            )

        existing_icon_urls = dict(
            MythicDungeonSpell.objects.filter(
                data_version=version,
                spell_id__in=spell_ids,
            ).values_list('spell_id', 'icon_url')
        )

        coverage = self._coverage(
            spell_ids,
            rows,
            misc,
            icon_names,
            wowhead_tooltips,
            wowhead_icon_names=wowhead_icon_names,
            existing_icon_urls=existing_icon_urls,
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
        icon_ratio = coverage['icon_name'] / max(1, coverage['total'])
        minimum_icon = max(
            0.0,
            min(1.0, float(options.get('min_icon_coverage') or 0)),
        )
        if icon_ratio < minimum_icon:
            raise CommandError(
                f'有效图标覆盖率 {icon_ratio:.1%} 低于最低要求 '
                f'{minimum_icon:.1%}，拒绝写库'
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
            wowhead_icon_names=wowhead_icon_names,
            coverage=coverage,
            listfile_url=listfile_url,
            wowhead_tooltips=wowhead_tooltips,
            tooltip_data_env=tooltip_data_env,
            tooltip_locale=tooltip_locale,
            tooltip_difficulty_id=tooltip_difficulty_id,
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

    def _resolve_wago_build(self, session, *, branch, configured_build):
        configured_build = str(configured_build or '').strip() or 'latest'
        if configured_build.lower() != 'latest':
            if not re.fullmatch(
                r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+',
                configured_build,
            ):
                raise CommandError(f'build 格式不正确: {configured_build}')
            return configured_build

        url = 'https://wago.tools/builds'
        for _page in range(12):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except Exception as exc:
                raise CommandError(f'解析 Wago 最新 {branch} build 失败: {exc}') from exc
            rows, next_url = self._parse_wago_builds_page(response.text)
            for row in rows:
                if str(row.get('product') or '').strip() != branch:
                    continue
                if row.get('processed') is False:
                    continue
                version = str(row.get('version') or '').strip()
                if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+', version):
                    return version
            if not next_url:
                break
            url = urljoin(url, next_url)
        raise CommandError(f'Wago builds 目录中找不到最新已处理分支: {branch}')

    @staticmethod
    def _parse_wago_builds_page(page_html):
        match = re.search(
            r'data-page=(?:"([^"]+)"|\'([^\']+)\')',
            str(page_html or ''),
        )
        if not match:
            raise CommandError('Wago builds 页面缺少 data-page 数据')
        try:
            payload = json.loads(html.unescape(match.group(1) or match.group(2) or ''))
        except (TypeError, ValueError) as exc:
            raise CommandError(f'Wago builds 页面数据无法解析: {exc}') from exc
        props = payload.get('props') if isinstance(payload, dict) else {}
        builds = props.get('builds') if isinstance(props, dict) else {}
        rows = builds.get('data') if isinstance(builds, dict) else []
        next_url = builds.get('next_page_url') if isinstance(builds, dict) else ''
        return (rows if isinstance(rows, list) else []), str(next_url or '').strip()

    @staticmethod
    def _resolve_dungeon_keys(version, configured_keys):
        requested = sorted({
            str(value or '').strip()
            for value in configured_keys
            if str(value or '').strip()
        })
        if not requested:
            return []
        existing = set(version.dungeons.filter(
            key__in=requested,
            is_active=True,
        ).values_list('key', flat=True))
        missing = sorted(set(requested) - existing)
        if missing:
            raise CommandError(
                f'数据版本 {version.key} 不包含活动地下城: {", ".join(missing)}',
            )
        return requested

    @staticmethod
    def _validate_tooltip_snapshot_context(version, spell_ids, *, branch, build):
        records = list(MythicDungeonSpell.objects.filter(
            data_version=version,
            spell_id__in=spell_ids,
        ).values('spell_id', 'source_branch', 'snapshot_build'))
        by_spell_id = {int(row['spell_id']): row for row in records}
        missing = sorted(set(spell_ids) - set(by_spell_id))
        mismatched = sorted(
            spell_id
            for spell_id, row in by_spell_id.items()
            if (
                str(row.get('source_branch') or '').strip() != branch
                or str(row.get('snapshot_build') or '').strip() != build
            )
        )
        if not missing and not mismatched:
            return
        details = []
        if missing:
            details.append(
                f'缺少技能快照 {len(missing)} 条（示例: {missing[:5]}）',
            )
        if mismatched:
            details.append(
                f'分支/build 不匹配 {len(mismatched)} 条（示例: {mismatched[:5]}）',
            )
        raise CommandError(
            'Tooltip-only 只能补充已经由目标 Wago DB2 快照初始化的技能；'
            + '；'.join(details)
            + '。请先去掉 --tooltip-only 执行完整 DB2 同步。'
        )

    @staticmethod
    def _resolve_wowhead_data_env(branch, configured):
        configured = int(configured or 0)
        if configured:
            if configured not in WOWHEAD_ENVIRONMENT_KEYS:
                raise CommandError(
                    f'不支持的 Wowhead dataEnv={configured}；'
                    f'可选值为 {sorted(WOWHEAD_ENVIRONMENT_KEYS)}'
                )
            return configured
        return WOWHEAD_DATA_ENV_BY_BRANCH.get(str(branch or '').strip(), 1)

    @staticmethod
    def _wowhead_spell_url(spell_id, data_env):
        environment = WOWHEAD_ENVIRONMENT_KEYS.get(int(data_env or 1), 'live')
        prefix = '' if environment == 'live' else f'/{environment}'
        return f'https://www.wowhead.com{prefix}/spell={spell_id}'

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
    def _collect_description_references(rows):
        result = set()
        for row in rows.values():
            for field in ('description_zh', 'aura_description_zh'):
                for match in DESCRIPTION_REFERENCE_RE.finditer(
                    str(row.get(field) or ''),
                ):
                    result.add(_to_int(match.group('spell_id')))
        return {spell_id for spell_id in result if spell_id > 0}

    @staticmethod
    def _collect_record_description_references(version, spell_ids):
        result = set()
        records = MythicDungeonSpell.objects.filter(
            data_version=version,
            spell_id__in=spell_ids,
        ).only('metadata')
        for record in records:
            metadata = record.metadata if isinstance(record.metadata, dict) else {}
            for field in ('raw_description_zh', 'raw_aura_description_zh'):
                for match in DESCRIPTION_REFERENCE_RE.finditer(
                    str(metadata.get(field) or ''),
                ):
                    result.add(_to_int(match.group('spell_id')))
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
    def _load_tooltip_icon_cache(path):
        result = {}
        if not path.is_file():
            return result
        with path.open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source)
            if 'IconName' not in (reader.fieldnames or []):
                return result
            for row in reader:
                spell_id = _to_int(row.get('SpellID'))
                icon_name = normalize_wowhead_icon_slug(row.get('IconName'))
                if spell_id:
                    result[spell_id] = icon_name
        return result

    @staticmethod
    def _write_tooltip_cache(path, cache, wowhead_icon_names=None):
        wowhead_icon_names = wowhead_icon_names or {}
        with path.open('w', encoding='utf-8', newline='') as output:
            writer = csv.DictWriter(
                output,
                fieldnames=['SpellID', 'DescriptionZh', 'IconName'],
            )
            writer.writeheader()
            for spell_id, description in sorted(cache.items()):
                writer.writerow({
                    'SpellID': spell_id,
                    'DescriptionZh': description,
                    'IconName': wowhead_icon_names.get(spell_id, ''),
                })

    def _resolve_wowhead_tooltips(
        self,
        spell_ids,
        cache,
        *,
        workers,
        delay,
        locale,
        data_env,
        difficulty_id,
        cache_path=None,
        wowhead_icon_names=None,
    ):
        missing = sorted(
            spell_id
            for spell_id in spell_ids
            if spell_id not in cache
            or self._tooltip_needs_retry(cache.get(spell_id))
            or (
                wowhead_icon_names is not None
                and spell_id not in wowhead_icon_names
            )
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
                    self._fetch_wowhead_tooltip_payload,
                    spell_id,
                    delay,
                    locale,
                    data_env=data_env,
                    difficulty_id=difficulty_id,
                ): spell_id
                for spell_id in missing
            }
            request_failures = 0
            for index, future in enumerate(as_completed(futures), start=1):
                spell_id = futures[future]
                try:
                    payload = future.result()
                    if payload is not None:
                        cache[spell_id] = payload['description']
                        if wowhead_icon_names is not None:
                            wowhead_icon_names[spell_id] = payload['icon_name']
                    else:
                        request_failures += 1
                except Exception:
                    request_failures += 1
                if index % 50 == 0 or index == len(futures):
                    if cache_path is not None:
                        self._write_tooltip_cache(
                            cache_path,
                            cache,
                            wowhead_icon_names,
                        )
                    found = sum(
                        bool(cache.get(requested_spell_id))
                        for requested_spell_id in spell_ids
                    )
                    self.stdout.write(
                        f'Wowhead tooltip 进度: {index}/{len(futures)}, '
                        f'found={found}, request_failures={request_failures}'
                    )
        return cache

    @staticmethod
    def _tooltip_needs_retry(description):
        text = str(description or '').strip()
        return '$' in text

    @staticmethod
    def _usable_tooltip_count(spell_ids, cache):
        return sum(
            bool(CJK_RE.search(str(cache.get(spell_id) or '')))
            for spell_id in spell_ids
        )

    @staticmethod
    def _localized_description(tooltip, db2_description):
        tooltip = str(tooltip or '').strip()
        db2_description = str(db2_description or '').strip()
        if CJK_RE.search(tooltip):
            return tooltip
        return db2_description or tooltip

    @staticmethod
    def _composite_description_zh(
        *,
        spell_id,
        raw_description_zh,
        raw_aura_description_zh,
        wowhead_tooltips,
        resolver,
    ):
        direct = str(wowhead_tooltips.get(spell_id) or '').strip()
        if CJK_RE.search(direct) and '$' not in direct:
            return {
                'description': direct,
                'source': SOURCE_WOWHEAD_TOOLTIP,
                'quality': QUALITY_RENDERED_EXTERNAL,
                'reference_spell_ids': [],
            }

        raw_text = str(
            raw_description_zh or raw_aura_description_zh or '',
        ).strip()
        reference_spell_ids = []

        def replace_reference(match):
            reference_spell_id = _to_int(match.group('spell_id'))
            referenced = str(
                wowhead_tooltips.get(reference_spell_id) or '',
            ).strip()
            if not CJK_RE.search(referenced) or '$' in referenced:
                return match.group(0)
            if reference_spell_id not in reference_spell_ids:
                reference_spell_ids.append(reference_spell_id)
            return referenced

        expanded = DESCRIPTION_REFERENCE_RE.sub(replace_reference, raw_text)
        description = resolver.resolve_mechanic(expanded, spell_id)
        if reference_spell_ids and description:
            return {
                'description': description,
                'source': SOURCE_WOWHEAD_TOOLTIP_REFERENCE,
                'quality': QUALITY_RENDERED_EXTERNAL,
                'reference_spell_ids': reference_spell_ids,
            }
        return {
            'description': description,
            'source': SOURCE_WAGO_DB2,
            'quality': QUALITY_MECHANIC_ONLY,
            'reference_spell_ids': [],
        }

    @staticmethod
    def _fetch_wowhead_tooltip(
        spell_id,
        delay,
        locale,
        *,
        data_env,
        difficulty_id=8,
        depth=0,
        seen=None,
    ):
        payload = Command._fetch_wowhead_tooltip_payload(
            spell_id,
            delay,
            locale,
            data_env=data_env,
            difficulty_id=difficulty_id,
            depth=depth,
            seen=seen,
        )
        if payload is None:
            return None
        return payload['description']

    @staticmethod
    def _fetch_wowhead_tooltip_payload(
        spell_id,
        delay,
        locale,
        *,
        data_env,
        difficulty_id=8,
        depth=0,
        seen=None,
    ):
        seen = set(seen or ())
        if spell_id in seen or depth > 2:
            return {'description': '', 'icon_name': ''}
        seen.add(spell_id)
        if delay:
            time.sleep(delay)
        url = f'https://nether.wowhead.com/tooltip/spell/{spell_id}'
        for attempt in range(3):
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    params={
                        'dataEnv': data_env,
                        'locale': locale,
                        'dd': difficulty_id,
                    },
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    },
                    proxies=_get_configured_proxies(),
                )
                if response.status_code == 404:
                    return {'description': '', 'icon_name': ''}
                response.raise_for_status()
                response_payload = response.json()
                icon_name = normalize_wowhead_icon_slug(
                    response_payload.get('icon'),
                )
                tooltip_html = str(response_payload.get('tooltip') or '')
                if not tooltip_html:
                    return {'description': '', 'icon_name': icon_name}
                description = Command._description_from_tooltip_html(tooltip_html)
                if description and '$spelldesc' not in description.lower():
                    return {
                        'description': description,
                        'icon_name': icon_name,
                    }
                for referenced_id in Command._referenced_spell_ids_from_tooltip(
                    tooltip_html,
                    spell_id,
                ):
                    referenced = Command._fetch_wowhead_tooltip_payload(
                        referenced_id,
                        0,
                        locale,
                        data_env=data_env,
                        difficulty_id=difficulty_id,
                        depth=depth + 1,
                        seen=seen,
                    )
                    if referenced and referenced['description']:
                        return {
                            'description': referenced['description'],
                            'icon_name': icon_name,
                        }
                return {
                    'description': re.sub(
                        r'^\$spelldesc(?=\D)',
                        '',
                        description,
                        flags=re.IGNORECASE,
                    ).strip(),
                    'icon_name': icon_name,
                }
            except Exception:
                if attempt < 2:
                    time.sleep(2 ** attempt)
        return None

    @staticmethod
    def _description_from_tooltip_html(tooltip_html):
        return description_from_tooltip_html(tooltip_html)

    @staticmethod
    def _referenced_spell_ids_from_tooltip(tooltip_html, source_spell_id):
        text = html.unescape(str(tooltip_html or ''))
        result = []
        for pattern in (
            r'/(?:[a-z]{2}|ptr|ptr2|beta)/spell=(\d+)',
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
        data_env,
        difficulty_id=8,
        dry_run=False,
    ):
        now = timezone.now()
        resolver = SpellTextResolver(
            locale='zhCN',
            branch=branch,
        )
        records = list(MythicDungeonSpell.objects.filter(
            data_version=version,
            spell_id__in=spell_ids,
        ))
        updated = []
        direct_count = 0
        reference_count = 0
        mechanic_count = 0
        blank_count = 0
        for record in records:
            metadata = dict(record.metadata or {})
            resolved = self._composite_description_zh(
                spell_id=record.spell_id,
                raw_description_zh=metadata.get('raw_description_zh'),
                raw_aura_description_zh=metadata.get('raw_aura_description_zh'),
                wowhead_tooltips=wowhead_tooltips,
                resolver=resolver,
            )
            description = str(resolved['description'] or '').strip()
            incoming_source = resolved['source']
            incoming_quality = resolved['quality']
            if should_preserve_description_for_snapshot(
                metadata,
                incoming_quality,
                full_build=build,
                difficulty_id=difficulty_id,
            ):
                continue

            for key in (
                'wowhead_tooltip_url',
                'wowhead_tooltip_source',
                'wowhead_reference_spell_ids',
                'wowhead_reference_sources',
                'wowhead_locale',
                'wowhead_data_env',
                'wowhead_environment',
                'wowhead_difficulty_id',
                'wowhead_version_scope',
                'wowhead_build_exact',
                'wowhead_requested_branch',
                'wowhead_requested_build',
            ):
                metadata.pop(key, None)
            reference_spell_ids = resolved['reference_spell_ids']
            if incoming_source == SOURCE_WOWHEAD_TOOLTIP:
                direct_count += 1
                metadata['wowhead_tooltip_url'] = self._wowhead_spell_url(
                    record.spell_id,
                    data_env,
                )
                metadata['wowhead_tooltip_source'] = (
                    f'https://nether.wowhead.com/tooltip/spell/{record.spell_id}'
                    f'?dataEnv={data_env}&locale={locale}&dd={difficulty_id}'
                )
            elif incoming_source == SOURCE_WOWHEAD_TOOLTIP_REFERENCE:
                reference_count += 1
                metadata['wowhead_reference_spell_ids'] = reference_spell_ids
                metadata['wowhead_reference_sources'] = [
                    (
                        f'https://nether.wowhead.com/tooltip/spell/{reference_spell_id}'
                        f'?dataEnv={data_env}&locale={locale}&dd={difficulty_id}'
                    )
                    for reference_spell_id in reference_spell_ids
                ]
            elif description:
                mechanic_count += 1
            else:
                blank_count += 1

            if incoming_source in (
                SOURCE_WOWHEAD_TOOLTIP,
                SOURCE_WOWHEAD_TOOLTIP_REFERENCE,
            ):
                metadata['wowhead_locale'] = locale
                metadata['wowhead_data_env'] = data_env
                metadata['wowhead_environment'] = WOWHEAD_ENVIRONMENT_KEYS[data_env]
                metadata['wowhead_difficulty_id'] = difficulty_id
                metadata['wowhead_version_scope'] = 'environment_current'
                metadata['wowhead_build_exact'] = False
                metadata['wowhead_requested_branch'] = branch
                metadata['wowhead_requested_build'] = build
            metadata.update(build_description_metadata(
                source=incoming_source,
                quality=incoming_quality,
            ))
            record.description_zh = description
            record.metadata = metadata
            record.updated_at = now
            updated.append(record)
        if updated and not dry_run:
            MythicDungeonSpell.objects.bulk_update(
                updated,
                [
                    'description_zh',
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
            bool(CJK_RE.search(str(wowhead_tooltips.get(spell_id) or '')))
            for spell_id in spell_ids
        )
        coverage['rendered_tooltip_reference_zh'] = reference_count
        coverage['mechanic_only_zh'] = mechanic_count
        coverage['blank_description_zh'] = blank_count
        spell_snapshot.update({
            'wowhead_tooltips': True,
            'wowhead_locale': locale,
            'wowhead_data_env': data_env,
            'wowhead_environment': WOWHEAD_ENVIRONMENT_KEYS[data_env],
            'wowhead_difficulty_id': difficulty_id,
            'wowhead_version_scope': 'environment_current',
            'wowhead_build_exact': False,
            'wowhead_requested_branch': branch,
            'wowhead_requested_build': build,
            'coverage': coverage,
            'synced_at': now.isoformat(),
        })
        version_metadata['spell_snapshot'] = spell_snapshot
        if not dry_run:
            version.metadata = version_metadata
            version.save(update_fields=['metadata', 'updated_at'])
        return {
            'fetched': coverage['rendered_tooltip_zh'],
            'referenced': reference_count,
            'mechanic_only': mechanic_count,
            'blank': blank_count,
            'updated': len(updated),
            'dry_run': bool(dry_run),
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
    def _coverage(
        spell_ids,
        rows,
        misc,
        icon_names,
        wowhead_tooltips,
        *,
        wowhead_icon_names=None,
        existing_icon_urls=None,
    ):
        wowhead_icon_names = wowhead_icon_names or {}
        existing_icon_urls = existing_icon_urls or {}
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
                bool(
                    icon_names.get(
                        _to_int(misc.get(spell_id, {}).get('SpellIconFileDataID')),
                    )
                    or wowhead_icon_names.get(spell_id)
                    or existing_icon_urls.get(spell_id)
                )
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
        tooltip_data_env,
        tooltip_locale=4,
        tooltip_difficulty_id=8,
        wowhead_icon_names=None,
    ):
        wowhead_icon_names = wowhead_icon_names or {}
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
        existing_spell_records = {
            int(record.spell_id): record
            for record in MythicDungeonSpell.objects.filter(
                data_version=version,
                spell_id__in=spell_ids,
            )
        }
        for spell_id in sorted(spell_ids):
            row = rows.get(spell_id, {})
            misc_row = misc.get(spell_id, {})
            file_data_id = _to_int(misc_row.get('SpellIconFileDataID')) or None
            wago_icon_name = normalize_wowhead_icon_slug(
                icon_names.get(file_data_id, '') if file_data_id else '',
            )
            wowhead_icon_name = normalize_wowhead_icon_slug(
                wowhead_icon_names.get(spell_id),
            )
            icon_name = wago_icon_name or wowhead_icon_name
            icon_url = build_wowhead_icon_url(icon_name)
            icon_source = (
                'wago_db2'
                if wago_icon_name
                else 'wowhead_tooltip' if wowhead_icon_name else ''
            )
            existing_record = existing_spell_records.get(spell_id)
            existing_icon_url = str(
                existing_record.icon_url if existing_record else ''
            ).strip()
            existing_asset_unavailable = bool(
                (existing_record.metadata or {}).get('asset_unavailable')
                if existing_record
                else False
            )
            preserve_missing_icon = bool(
                existing_record is not None
                and not icon_name
                and existing_icon_url
            )
            preserve_asset = (
                preserve_missing_icon
                or (
                    existing_record is not None
                    and existing_record.icon_name == icon_name
                    and (
                        existing_asset_unavailable
                        or (
                            existing_icon_url
                            and not self._is_wowhead_asset_url(existing_icon_url)
                        )
                    )
                )
            )
            if preserve_missing_icon:
                file_data_id = existing_record.icon_file_data_id
                icon_name = existing_record.icon_name
                icon_source = str(
                    (existing_record.metadata or {}).get('icon_source')
                    or 'preserved_existing'
                )
            if preserve_asset:
                icon_url = existing_icon_url
            description = resolver_en.resolve(row.get('description'), spell_id)
            resolved_description = self._composite_description_zh(
                spell_id=spell_id,
                raw_description_zh=row.get('description_zh'),
                raw_aura_description_zh=row.get('aura_description_zh'),
                wowhead_tooltips=wowhead_tooltips,
                resolver=resolver_zh,
            )
            description_zh = resolved_description['description']
            incoming_source = resolved_description['source']
            incoming_quality = resolved_description['quality']
            reference_spell_ids = resolved_description['reference_spell_ids']
            aura_description = resolver_en.resolve(
                row.get('aura_description'),
                spell_id,
            )
            aura_description_zh = resolver_zh.resolve_mechanic(
                row.get('aura_description_zh'),
                spell_id,
            )
            existing_metadata = dict(
                existing_record.metadata or {},
            ) if existing_record else {}
            preserve_description = bool(
                existing_record
                and existing_record.description_zh
                and should_preserve_description_for_snapshot(
                    existing_metadata,
                    incoming_quality,
                    full_build=build,
                    difficulty_id=tooltip_difficulty_id,
                )
            )
            if preserve_description:
                description_zh = existing_record.description_zh
            metadata = {
                'source': 'wago.tools DB2',
                'source_branch': branch,
                'snapshot_build': build,
                'raw_description': row.get('description', ''),
                'raw_description_zh': row.get('description_zh', ''),
                'raw_aura_description': row.get('aura_description', ''),
                'raw_aura_description_zh': row.get('aura_description_zh', ''),
                'icon_source': icon_source,
                'listfile_url': listfile_url,
                'coverage': coverage,
            }
            if icon_source == 'wowhead_tooltip':
                metadata['wowhead_icon_source'] = (
                    f'https://nether.wowhead.com/tooltip/spell/{spell_id}'
                    f'?dataEnv={tooltip_data_env}&locale={tooltip_locale}'
                    f'&dd={tooltip_difficulty_id}'
                )
            if incoming_source == SOURCE_WOWHEAD_TOOLTIP:
                metadata['wowhead_tooltip_url'] = self._wowhead_spell_url(
                    spell_id,
                    tooltip_data_env,
                )
                metadata['wowhead_tooltip_source'] = (
                    f'https://nether.wowhead.com/tooltip/spell/{spell_id}'
                    f'?dataEnv={tooltip_data_env}&locale={tooltip_locale}'
                    f'&dd={tooltip_difficulty_id}'
                )
            elif incoming_source == SOURCE_WOWHEAD_TOOLTIP_REFERENCE:
                metadata['wowhead_reference_spell_ids'] = reference_spell_ids
                metadata['wowhead_reference_sources'] = [
                    (
                        f'https://nether.wowhead.com/tooltip/spell/{reference_spell_id}'
                        f'?dataEnv={tooltip_data_env}&locale={tooltip_locale}'
                        f'&dd={tooltip_difficulty_id}'
                    )
                    for reference_spell_id in reference_spell_ids
                ]
            if incoming_source in (
                SOURCE_WOWHEAD_TOOLTIP,
                SOURCE_WOWHEAD_TOOLTIP_REFERENCE,
            ):
                metadata['wowhead_locale'] = tooltip_locale
                metadata['wowhead_data_env'] = tooltip_data_env
                metadata['wowhead_environment'] = WOWHEAD_ENVIRONMENT_KEYS[
                    tooltip_data_env
                ]
                metadata['wowhead_difficulty_id'] = tooltip_difficulty_id
                metadata['wowhead_version_scope'] = 'environment_current'
                metadata['wowhead_build_exact'] = False
            metadata.update(build_description_metadata(
                source=incoming_source,
                quality=incoming_quality,
            ))
            if preserve_asset:
                metadata.update({
                    key: value
                    for key, value in (existing_record.metadata or {}).items()
                    if str(key).startswith('asset_')
                })
            if preserve_description:
                preserve_description_provenance(metadata, existing_metadata)
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
                    'metadata': metadata,
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
        spell_snapshot = dict(version_metadata.get('spell_snapshot') or {})
        description_coverage = {
            QUALITY_EXACT_RENDERED: 0,
            QUALITY_RENDERED_EXTERNAL: 0,
            QUALITY_MECHANIC_ONLY: 0,
        }
        for record in spell_records.values():
            quality = description_quality(record.metadata)
            if quality in description_coverage and record.description_zh:
                description_coverage[quality] += 1
        spell_snapshot.update({
            'source': 'wago.tools DB2',
            'source_branch': branch,
            'source_locale': 'zhCN',
            'snapshot_build': build,
            'listfile_url': listfile_url,
            'wowhead_tooltips': bool(
                self._usable_tooltip_count(spell_ids, wowhead_tooltips)
            ),
            'wowhead_data_env': tooltip_data_env,
            'wowhead_environment': WOWHEAD_ENVIRONMENT_KEYS[tooltip_data_env],
            'wowhead_difficulty_id': tooltip_difficulty_id,
            'wowhead_version_scope': 'environment_current',
            'wowhead_build_exact': False,
            'coverage': coverage,
            'description_coverage': description_coverage,
            'synced_at': now.isoformat(),
        })
        version_metadata['spell_snapshot'] = spell_snapshot
        version.metadata = version_metadata
        version.save(update_fields=['metadata', 'updated_at'])
        return {
            'spells': len(spell_records),
            'links': links,
            'effects': len(effects) * 2,
        }

    @staticmethod
    def _is_wowhead_asset_url(value):
        return urlsplit(str(value or '').strip()).netloc.lower() == 'wow.zamimg.com'


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
