# -*- coding: utf-8 -*-
"""从 Wowhead 服务端 Tooltip 增量同步天赋展示元数据。"""

from __future__ import annotations

import html
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.mythic_planner.icon_assets import normalize_wowhead_icon_slug
from botend.services.article_image_service import _get_configured_proxies


WOWHEAD_ENVIRONMENTS = {
    1: 'live',
}
WOWHEAD_DATA_ENV_BY_TALENT_BRANCH = {
    'retail': 1,
}
CACHE_SCHEMA_VERSION = 1
CJK_RE = re.compile(r'[\u3400-\u9fff]')
UNRESOLVED_X_RE = re.compile(r'(?<![A-Za-z])x(?![A-Za-z])', re.IGNORECASE)
INVALID_DESCRIPTION_TOKENS = ('$', '@spell', '<', '|c', '|C', '|r', '|R')
ICON_SLUG_RE = re.compile(r'[a-z0-9_.-]+')


class Command(BaseCommand):
    help = '从 Wowhead 正式服 Tooltip 增量同步正式服天赋中文名称、说明和图标'

    def add_arguments(self, parser):
        parser.add_argument(
            '--version-key',
            action='append',
            default=[],
            help='正式服 WowTalentVersion.key；可重复，默认处理全部有节点的生效正式服版本',
        )
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--wowhead-locale', type=int, default=4, help='Wowhead语言编号，简中为4')
        parser.add_argument(
            '--cache-dir',
            default='.cache/wowhead_talent_metadata',
            help='缓存目录；内部继续按版本、环境和语言隔离',
        )
        parser.add_argument('--refresh', action='store_true', help='重新请求已有缓存；失败时保留旧缓存')
        parser.add_argument('--workers', type=int, default=6, help='Tooltip请求并发数')
        parser.add_argument('--delay', type=float, default=0.05, help='每个请求前的延迟秒数')
        parser.add_argument('--limit', type=int, default=0, help='每个版本最多处理的唯一技能数，0为不限')
        parser.add_argument(
            '--include-hero-anchors',
            action='store_true',
            help='包含不直接展示的英雄锚点；默认排除',
        )
        parser.add_argument('--dry-run', action='store_true', help='允许更新本地缓存，但不写数据库')

    def handle(self, *args, **options):
        versions = self._resolve_versions(options.get('version_key'))
        version_environments = []
        for version in versions:
            expected_env = self._expected_data_env(version)
            version_environments.append((version, expected_env))

        cache_dir = Path(str(options.get('cache_dir') or '.cache/wowhead_talent_metadata'))
        if not cache_dir.is_absolute():
            cache_dir = Path(settings.BASE_DIR) / cache_dir

        proxies = _get_configured_proxies()
        self.stdout.write(
            'Wowhead 请求代理: '
            + ('使用项目代理配置' if proxies else '未显式配置，遵循系统环境或直连')
        )
        self.stdout.write(
            '注意：Wowhead live Tooltip 对应正式服当前数据，不是可固定到目标 build 的历史快照。'
        )

        totals = Counter()
        for version, data_env in version_environments:
            result = self._sync_version(
                version=version,
                data_env=data_env,
                locale=max(0, int(options.get('wowhead_locale') or 4)),
                cache_dir=cache_dir,
                class_name=str(options.get('class_name') or '').strip(),
                include_hero_anchors=bool(options.get('include_hero_anchors')),
                refresh=bool(options.get('refresh')),
                workers=max(1, int(options.get('workers') or 1)),
                delay=max(0.0, float(options.get('delay') or 0.0)),
                limit=max(0, int(options.get('limit') or 0)),
                dry_run=bool(options.get('dry_run')),
                proxies=proxies,
            )
            totals.update(result)

        self.stdout.write(self.style.SUCCESS(
            '全部版本完成: '
            f'versions={len(versions)}, rows={totals["rows"]}, '
            f'updated={totals["updated_rows"]}, '
            f'descriptions={totals["description_zh"]}, '
            f'names={totals["name_zh"]}, icons={totals["icon"]}, '
            f'request_failed={totals["request_failed"]}, '
            f'dry_run={bool(options.get("dry_run"))}'
        ))

    def _resolve_versions(self, version_keys):
        if isinstance(version_keys, str):
            version_keys = [version_keys]
        keys = list(dict.fromkeys(
            str(key or '').strip() for key in (version_keys or []) if str(key or '').strip()
        ))
        if keys:
            versions = list(WowTalentVersion.objects.filter(key__in=keys))
            found = {version.key for version in versions}
            missing = [key for key in keys if key not in found]
            if missing:
                raise CommandError(f'找不到天赋版本: {", ".join(missing)}')
            by_key = {version.key: version for version in versions}
            ordered = [by_key[key] for key in keys]
            unsupported = [
                version.key for version in ordered
                if str(version.branch or '').strip().lower() != 'retail'
            ]
            if unsupported:
                raise CommandError(
                    'Wowhead PTR 当前没有可用的简中正文，本命令只支持正式服版本；'
                    f'不支持: {", ".join(unsupported)}'
                )
            return ordered

        versions = list(
            WowTalentVersion.objects.filter(
                is_active=True,
                branch='retail',
                nodes__isnull=False,
            ).distinct().order_by('key')
        )
        if not versions:
            raise CommandError('找不到含节点的生效正式服天赋版本，请使用 --version-key 明确指定')
        return versions

    @staticmethod
    def _expected_data_env(version):
        branch = str(version.branch or '').strip().lower()
        data_env = WOWHEAD_DATA_ENV_BY_TALENT_BRANCH.get(branch)
        if not data_env:
            raise CommandError(
                f'天赋版本 {version.key} 的 branch={version.branch!r} 不受支持；'
                'Wowhead PTR 当前没有可用简中正文，本命令不使用 beta 代替 PTR'
            )
        return data_env

    def _sync_version(
        self,
        *,
        version,
        data_env,
        locale,
        cache_dir,
        class_name,
        include_hero_anchors,
        refresh,
        workers,
        delay,
        limit,
        dry_run,
        proxies,
    ):
        queryset = WowTalentNodeMetadata.objects.filter(talent_version=version)
        if not include_hero_anchors:
            queryset = queryset.exclude(tree_type='hero_anchor')
        if class_name:
            queryset = queryset.filter(class_name=class_name)
        rows = list(queryset.order_by('id'))
        if not rows:
            raise CommandError(f'天赋版本 {version.key} 没有符合条件的节点')

        spell_ids = sorted({
            self._display_spell_id(row)
            for row in rows
            if self._display_spell_id(row) > 0
        })
        if limit:
            spell_ids = spell_ids[:limit]
            selected = set(spell_ids)
            rows = [row for row in rows if self._display_spell_id(row) in selected]
        if not spell_ids:
            raise CommandError(f'天赋版本 {version.key} 没有可查询的展示技能')

        cache_path = self._cache_path(
            cache_dir,
            version_key=version.key,
            data_env=data_env,
            locale=locale,
        )
        cache = self._load_cache(
            cache_path,
            version_key=version.key,
            data_env=data_env,
            locale=locale,
        )
        records = cache['records']
        targets = spell_ids if refresh else [
            spell_id for spell_id in spell_ids if str(spell_id) not in records
        ]

        self.stdout.write(
            f'[{version.key}] branch={version.branch}, '
            f'env={WOWHEAD_ENVIRONMENTS[data_env]}({data_env}), locale={locale}, '
            f'rows={len(rows)}, unique_spells={len(spell_ids)}, '
            f'cached={len(spell_ids) - len(targets)}, fetch={len(targets)}'
        )

        fetch_stats = self._fill_cache(
            targets,
            cache=cache,
            cache_path=cache_path,
            data_env=data_env,
            locale=locale,
            workers=workers,
            delay=delay,
            proxies=proxies,
        )
        candidate_stats = Counter(
            str((records.get(str(spell_id)) or {}).get('status') or 'uncached')
            for spell_id in spell_ids
        )
        updates, update_stats, samples = self._build_updates(
            rows,
            records,
            environment=WOWHEAD_ENVIRONMENTS[data_env],
        )

        self.stdout.write(
            f'[{version.key}] result={dict(sorted(candidate_stats.items()))}, '
            f'fetch={dict(fetch_stats)}, planned_rows={len(updates)}, '
            f'fields={dict(update_stats)}'
        )
        if samples:
            self.stdout.write(
                f'[{version.key}] samples='
                + json.dumps(samples, ensure_ascii=False)[:3000]
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'[{version.key}] DRY RUN：仅允许更新本地缓存，数据库未写入'
            ))
        elif updates:
            now = timezone.now()
            for row in updates:
                row.last_updated = now
            with transaction.atomic():
                WowTalentNodeMetadata.objects.bulk_update(
                    updates,
                    ['name_zh', 'description_zh', 'icon', 'source', 'last_updated'],
                    batch_size=500,
                )
            self.stdout.write(self.style.SUCCESS(
                f'[{version.key}] 已更新 {len(updates)} 条节点'
            ))
        else:
            self.stdout.write(f'[{version.key}] 数据库无需更新')

        return Counter({
            'rows': len(rows),
            'updated_rows': len(updates),
            'name_zh': update_stats['name_zh'],
            'description_zh': update_stats['description_zh'],
            'icon': update_stats['icon'],
            'request_failed': fetch_stats['request_failed'],
        })

    def _fill_cache(
        self,
        spell_ids,
        *,
        cache,
        cache_path,
        data_env,
        locale,
        workers,
        delay,
        proxies,
    ):
        stats = Counter()
        if not spell_ids:
            return stats

        records = cache['records']
        dirty = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._fetch_tooltip,
                    spell_id,
                    data_env=data_env,
                    locale=locale,
                    delay=delay,
                    proxies=proxies,
                ): spell_id
                for spell_id in spell_ids
            }
            for future in as_completed(futures):
                spell_id = futures[future]
                try:
                    record = future.result()
                except Exception:
                    record = None
                if record is None:
                    stats['request_failed'] += 1
                    continue
                records[str(spell_id)] = record
                stats[f'status_{record["status"]}'] += 1
                dirty += 1
                if dirty >= 50:
                    self._write_cache(cache_path, cache)
                    dirty = 0
        if dirty:
            self._write_cache(cache_path, cache)
        return stats

    @classmethod
    def _fetch_tooltip(cls, spell_id, *, data_env, locale, delay, proxies):
        if delay:
            time.sleep(delay)
        url = f'https://nether.wowhead.com/tooltip/spell/{int(spell_id)}'
        for attempt in range(3):
            try:
                response = requests.get(
                    url,
                    params={'dataEnv': data_env, 'locale': locale},
                    timeout=45,
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    },
                    proxies=proxies,
                )
                if response.status_code == 404:
                    return cls._cache_record(status='missing')
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError('Wowhead Tooltip 响应不是对象')
                return cls._record_from_payload(payload)
            except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
        return None

    @classmethod
    def _record_from_payload(cls, payload):
        raw_name = str(payload.get('name') or '').strip()
        raw_icon = normalize_wowhead_icon_slug(payload.get('icon'))
        raw_description = cls._description_from_tooltip_html(payload.get('tooltip'))

        name_zh = raw_name if cls._is_valid_zh_name(raw_name) else ''
        description_zh = (
            raw_description if cls._is_valid_zh_description(raw_description) else ''
        )
        icon = raw_icon if ICON_SLUG_RE.fullmatch(raw_icon or '') else ''
        if description_zh:
            status = 'ok'
        elif name_zh or icon:
            status = 'partial'
        elif raw_name or raw_description:
            status = 'unlocalized'
        else:
            status = 'empty'
        return cls._cache_record(
            status=status,
            name_zh=name_zh,
            description_zh=description_zh,
            icon=icon,
            has_raw_name=bool(raw_name),
            has_raw_description=bool(raw_description),
        )

    @staticmethod
    def _cache_record(
        *,
        status,
        name_zh='',
        description_zh='',
        icon='',
        has_raw_name=False,
        has_raw_description=False,
    ):
        return {
            'status': status,
            'name_zh': name_zh,
            'description_zh': description_zh,
            'icon': icon,
            'has_raw_name': bool(has_raw_name),
            'has_raw_description': bool(has_raw_description),
            'fetched_at': timezone.now().isoformat(),
        }

    @staticmethod
    def _description_from_tooltip_html(tooltip_html):
        match = re.search(
            r'<div\s+class="q">(.*?)</div>',
            str(tooltip_html or ''),
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ''
        value = re.sub(r'<br\s*/?>', '\n', match.group(1), flags=re.IGNORECASE)
        value = re.sub(r'<[^>]+>', '', value)
        value = html.unescape(value)
        value = value.replace('\u200b', '').replace('\ufeff', '')
        value = re.sub(r'(?<=\d)\$', '%', value)
        return re.sub(r'\s+', ' ', value).strip()

    @staticmethod
    def _is_valid_zh_name(value):
        return bool(value and CJK_RE.search(str(value)))

    @staticmethod
    def _is_valid_zh_description(value):
        value = str(value or '').strip()
        if not value or not CJK_RE.search(value):
            return False
        if any(token in value for token in INVALID_DESCRIPTION_TOKENS):
            return False
        return not UNRESOLVED_X_RE.search(value)

    @classmethod
    def _build_updates(cls, rows, records, *, environment):
        updates = []
        stats = Counter()
        samples = []
        source = f'db2+wowhead_{environment}'[:32]
        for row in rows:
            record = records.get(str(cls._display_spell_id(row))) or {}
            changed = []
            for field in ('name_zh', 'description_zh', 'icon'):
                value = str(record.get(field) or '').strip()
                if not value or getattr(row, field) == value:
                    continue
                if field == 'name_zh' and not cls._is_valid_zh_name(value):
                    continue
                if field == 'description_zh' and not cls._is_valid_zh_description(value):
                    continue
                if field == 'icon' and not ICON_SLUG_RE.fullmatch(value):
                    continue
                setattr(row, field, value)
                changed.append(field)
                stats[field] += 1
            if not changed:
                continue
            row.source = source
            updates.append(row)
            if len(samples) < 20:
                samples.append({
                    'row_id': row.id,
                    'spell_id': cls._display_spell_id(row),
                    'changed': changed,
                    'name_zh': row.name_zh,
                })
        stats['updated_rows'] = len(updates)
        return updates, stats, samples

    @staticmethod
    def _display_spell_id(row):
        try:
            return int(row.display_spell_id or row.spell_id or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _cache_path(cache_dir, *, version_key, data_env, locale):
        safe_key = re.sub(r'[^A-Za-z0-9._-]+', '_', str(version_key or '')).strip('._')
        if not safe_key:
            safe_key = 'unknown-version'
        return Path(cache_dir) / safe_key / f'dataenv{data_env}_locale{locale}.json'

    @staticmethod
    def _load_cache(path, *, version_key, data_env, locale):
        empty = {
            'schema_version': CACHE_SCHEMA_VERSION,
            'version_key': version_key,
            'data_env': int(data_env),
            'locale': int(locale),
            'records': {},
        }
        if not path.is_file():
            return empty
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(payload, dict):
            return empty
        if (
            payload.get('schema_version') != CACHE_SCHEMA_VERSION
            or payload.get('version_key') != version_key
            or int(payload.get('data_env') or 0) != int(data_env)
            or int(payload.get('locale') or -1) != int(locale)
            or not isinstance(payload.get('records'), dict)
        ):
            return empty
        return payload

    @staticmethod
    def _write_cache(path, cache):
        path.parent.mkdir(parents=True, exist_ok=True)
        cache['updated_at'] = timezone.now().isoformat()
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
            encoding='utf-8',
        )
        temporary.replace(path)
