"""定向补齐活动装备目录中缺失的装等属性与特效 Tooltip。"""

from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.management.commands.sync_gear_builder_catalog import Command as SyncCommand
from botend.models import SeasonMeta, WowItemVariantSnapshot
from botend.services.gear_builder_catalog_source import CatalogSourceError, CurrentGearCatalogSource


EQUIPMENT_TYPES = {
    WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
    WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
}


def _needs_effect(row):
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    description = f'{row.item.description_zh}\n{row.item.description}'.casefold()
    return bool(
        row.item.slot_key == 'trinket'
        or row.item.effect_refs
        or metadata.get('requires_effect_mapping')
        or any(prefix in description for prefix in ('装备：', '使用：', 'equip:', 'use:'))
    )


class Command(BaseCommand):
    help = '仅抓取活动装备目录中缺少的属性、特效、名称或图标，并刷新完整性审计。'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='只列出待补齐数量，不请求远端或写数据库')
        parser.add_argument('--force', action='store_true', help='重新抓取活动批次的全部装备 Tooltip')
        parser.add_argument('--workers', type=int, default=8, help='Wowhead 并发数，默认 8，最大 24')
        parser.add_argument('--timeout', type=int, default=45, help='单次远端请求超时秒数')
        parser.add_argument('--cache-dir', default='.cache/gear_builder', help='Wowhead Tooltip 缓存目录')
        parser.add_argument('--refresh-cache', action='store_true', help='忽略仍在有效期内的缓存')
        parser.add_argument('--cache-ttl-hours', type=int, default=6, help='缓存有效小时数，默认 6')
        parser.add_argument('--no-proxy', action='store_true', help='忽略服务端代理环境变量')

    def handle(self, *args, **options):
        season = SeasonMeta.objects.filter(is_active=True).exclude(gear_batch_key='').order_by('-id').first()
        if season is None:
            raise CommandError('没有已激活的装备目录批次。')
        rows = list(WowItemVariantSnapshot.objects.filter(
            season=season,
            batch_key=season.gear_batch_key,
            variant_type__in=EQUIPMENT_TYPES,
        ).select_related('item'))
        targets = defaultdict(list)
        for row in rows:
            item_missing = not row.item.name_zh or not row.item.icon
            variant_missing = not row.stats_json and not row.effects_json
            effect_missing = not row.effects_json and _needs_effect(row)
            if options['force'] or item_missing or variant_missing or effect_missing:
                targets[(int(row.item.item_id), int(row.item_level or 0))].append(row)
        summary = {
            'season_key': season.season_key,
            'batch_key': season.gear_batch_key,
            'equipment_variants': len(rows),
            'target_tooltips': len(targets),
            'target_variants': sum(len(group) for group in targets.values()),
        }
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        if options['dry_run'] or not targets:
            message = 'dry-run 未请求远端或写数据库。' if options['dry_run'] else '活动批次没有需要补齐的装备数据。'
            self.stdout.write(self.style.SUCCESS(message))
            return

        source = CurrentGearCatalogSource(
            cache_dir=options['cache_dir'],
            workers=options['workers'],
            timeout=options['timeout'],
            no_proxy=options['no_proxy'],
            progress=self.stdout.write,
            refresh_wowhead_cache=options['refresh_cache'],
            wowhead_cache_ttl_hours=options['cache_ttl_hours'],
        )
        cache_dir = source.cache_root / (season.game_build or 'unknown') / 'wowhead'
        cache_dir.mkdir(parents=True, exist_ok=True)
        fetched = {}
        failures = {}
        with ThreadPoolExecutor(max_workers=source.workers) as executor:
            futures = {
                executor.submit(source._wowhead_tooltip, item_id, item_level, cache_dir): (item_id, item_level)
                for item_id, item_level in targets
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    details = future.result()
                    if any(details.get(field) for field in ('name_zh', 'name', 'icon', 'stats', 'effects')):
                        fetched[key] = details
                    else:
                        failures[key] = '远端响应没有可补齐的名称、图标、属性或特效。'
                except CatalogSourceError as exc:
                    failures[key] = str(exc)
        if not fetched:
            raise CommandError(f'待补齐的 {len(targets)} 组 Tooltip 全部抓取失败，数据库未修改。')

        updated_variants = 0
        updated_items = set()
        processed_item_ids = set()
        with transaction.atomic():
            for key in sorted(fetched, key=lambda value: (value[0], -value[1])):
                details = fetched[key]
                for row in targets[key]:
                    update_fields = []
                    if details.get('stats') and (options['force'] or not row.stats_json):
                        row.stats_json = details['stats']
                        update_fields.append('stats_json')
                    if details.get('effects') and (options['force'] or not row.effects_json):
                        row.effects_json = details['effects']
                        update_fields.append('effects_json')
                    if details.get('primary_options'):
                        metadata = dict(row.metadata or {})
                        metadata['primary_stat_values'] = details['primary_options']
                        row.metadata = metadata
                        update_fields.append('metadata')
                    if update_fields:
                        row.save(update_fields=tuple(dict.fromkeys(update_fields)))
                        updated_variants += 1
                    item = row.item
                    item_fields = []
                    if item.item_id not in processed_item_ids:
                        processed_item_ids.add(item.item_id)
                        for field in ('name_zh', 'name', 'description_zh', 'description', 'icon'):
                            value = details.get(field)
                            if value and (options['force'] or not getattr(item, field)):
                                setattr(item, field, value)
                                item_fields.append(field)
                        if details.get('quality') and (options['force'] or not item.quality):
                            item.quality = details['quality']
                            item_fields.append('quality')
                    if item_fields:
                        item.updated_at = timezone.now()
                        item_fields.append('updated_at')
                        item.save(update_fields=tuple(dict.fromkeys(item_fields)))
                        updated_items.add(item.pk)

            audit = SyncCommand()._audit_batch(season, season.gear_batch_key)
            audit['tooltip_repair'] = {
                'requested': len(targets),
                'fetched': len(fetched),
                'failed': len(failures),
                'updated_variants': updated_variants,
                'updated_items': len(updated_items),
                'repaired_at': timezone.now().isoformat(),
            }
            season.gear_sync_report = audit
            season.gear_sync_status = 'ready' if not audit['blocking_errors'] else 'invalid'
            season.gear_synced_at = timezone.now()
            season.save(update_fields=(
                'gear_sync_report', 'gear_sync_status', 'gear_synced_at', 'updated_at',
            ))

        result = {
            **summary,
            'fetched': len(fetched),
            'failed': len(failures),
            'updated_variants': updated_variants,
            'updated_items': len(updated_items),
            'remaining_missing': audit.get('missing_counts') or {},
        }
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        if failures:
            self.stderr.write(self.style.WARNING(f'{len(failures)} 组抓取失败，可重复执行命令续补。'))
        self.stdout.write(self.style.SUCCESS('活动装备目录 Tooltip 补齐与审计已完成。'))
