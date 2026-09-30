"""为现有活动目录补充晋升毒液石英雄八阶、神话八阶。"""
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.management.commands.sync_gear_builder_catalog import Command as SyncCommand
from botend.models import SeasonMeta, WowItemVariantSnapshot
from botend.services.gear_builder import active_season
from botend.services.gear_builder_catalog_source import CurrentGearCatalogSource, CatalogSourceError
from botend.services.gear_builder_venomstone import SOURCE_BUILD, upgraded_variant, apply_tooltip, preserve_localized_effects, tooltip_branch
from botend.services.season_keys import canonical_season_key


def base_payload(row):
    return {
        'key': row.variant_key, 'type': row.variant_type, 'item_level': row.item_level,
        'upgrade_track': row.upgrade_track, 'track_rank': row.track_rank, 'track_max_rank': row.track_max_rank,
        'bonus_ids': row.bonus_ids, 'compatible_slots': row.compatible_slots,
        'socket_count': row.socket_count, 'socket_types': row.socket_types,
        'sources': row.source_json, 'unique_group': row.unique_group,
        'max_equipped': row.max_equipped, 'is_intrinsic_embellishment': row.is_intrinsic_embellishment,
        'metadata': deepcopy(row.metadata or {}),
    }


class Command(BaseCommand):
    help = '补充第二赛季毒液石英雄 8/6（328）、神话 8/6（340）；默认预览，--apply 写入'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='抓取目标装等属性后写入活动批次')
        parser.add_argument('--workers', type=int, default=6, help='并发请求数')
        parser.add_argument('--timeout', type=int, default=30, help='单次请求超时秒数')
        parser.add_argument('--cache-dir', default='.cache/gear_builder/venomstone', help='提示缓存目录')
        parser.add_argument('--refresh-cache', action='store_true', help='重新请求目标装等提示')
        parser.add_argument('--no-proxy', action='store_true', help='忽略配置代理')
        parser.add_argument('--tooltip-branch', choices=('auto', 'live', 'ptr', 'ptr-2'), default='auto',
                            help='默认按装备来源选择：普通装备使用正式服，预览装备使用 ptr-2')

    def handle(self, *args, **options):
        season = active_season()
        if not season or not season.gear_batch_key or canonical_season_key(season.season_key) != 'mn-s2':
            raise CommandError('活动装备目录必须为至暗之夜第二赛季。')
        rows = list(WowItemVariantSnapshot.objects.filter(
            season=season, batch_key=season.gear_batch_key, variant_type='drop_equipment',
            track_rank=6, track_max_rank=6, upgrade_track__in=('hero', 'myth'),
        ).select_related('item'))
        targets = [(row, upgraded_variant(row.item.inventory_type, base_payload(row))) for row in rows]
        targets = [(row, payload) for row, payload in targets if payload]
        # PTR 会持续调数值；只新增八阶会把旧 SimC 构建与当前提示混在一起。
        # 同步刷新这些预览装备的全部已有掉落档位，包含勇士及特殊高阶。
        preview_items = {row.item_id for row, _ in targets
                         if (row.metadata or {}).get('ptr_preview') or (row.item.metadata or {}).get('ptr_preview')}
        existing = list(WowItemVariantSnapshot.objects.filter(
            season=season, batch_key=season.gear_batch_key, variant_type='drop_equipment',
            item_id__in=preview_items,
        ).select_related('item'))
        upgrade_keys = {(row.item_id, payload['key']) for row, payload in targets}
        refresh = [(row, base_payload(row)) for row in existing
                   if (row.item_id, row.variant_key) not in upgrade_keys]
        updates = targets + refresh
        report = {'season': season.season_key, 'batch': season.gear_batch_key,
                  'variants': len(targets), 'refreshed_variants': len(refresh), 'hero_level': 328, 'myth_level': 340}
        if not options['apply']:
            self.stdout.write(json.dumps(report, ensure_ascii=False))
            self.stdout.write('仅预览，未请求远端或写入数据库；添加 --apply 执行。')
            return
        source = CurrentGearCatalogSource(
            cache_dir=options['cache_dir'], workers=options['workers'], timeout=options['timeout'],
            no_proxy=options['no_proxy'], refresh_wowhead_cache=options['refresh_cache'],
        )
        def target_key(row, payload):
            branch = tooltip_branch({**(row.item.metadata or {}), **(row.metadata or {})}, options['tooltip_branch'])
            return row.item.item_id, payload['item_level'], branch
        keys = sorted({target_key(row, payload) for row, payload in updates})
        self.stdout.write(f'正在补全 {len(keys)} 组装等提示（含 {len(refresh)} 个预览装备已有档位），全部验证通过后写入。')
        branch_counts = {branch: sum(key[2] == branch for key in keys) for branch in sorted({key[2] for key in keys})}
        self.stdout.write('数据来源：' + '，'.join(f'{branch} {count} 组' for branch, count in branch_counts.items()))
        def fetch(key):
            item_id, item_level, branch = key
            return key, source.venomstone_tooltip(item_id, item_level, source.cache_root, branch)
        try:
            with ThreadPoolExecutor(max_workers=source.workers) as pool:
                details = dict(pool.map(fetch, keys))
            failures = []
            for row, payload in updates:
                key = target_key(row, payload)
                try:
                    apply_tooltip(payload, details[key],
                                  requires_effect=bool(row.effects_json or row.item.effect_refs) or row.item.inventory_type == 12)
                    preserve_localized_effects(payload, row.effects_json or [])
                except ValueError as exc:
                    failures.append(f'{row.item.name_zh or row.item.name}（物品 {key[0]}，{payload["upgrade_track"]} {payload["track_rank"]}/{payload["track_max_rank"]}，装等 {key[1]}，来源 {key[2]}）：{exc}')
            if failures:
                raise ValueError(f'{len(failures)} 个变体未通过校验：\n' + '\n'.join(failures))
        except (CatalogSourceError, ValueError) as exc:
            raise CommandError(f'目标装等数据不完整，数据库未修改：{exc}') from exc
        created = 0
        with transaction.atomic():
            current = SeasonMeta.objects.select_for_update().get(pk=season.pk)
            if not current.is_active or current.gear_batch_key != season.gear_batch_key or active_season().pk != season.pk:
                raise CommandError('活动目录已切换，请重新执行。')
            for row, payload in targets:
                _variant, added = WowItemVariantSnapshot.objects.update_or_create(
                    season=current, batch_key=current.gear_batch_key, item=row.item, variant_key=payload['key'],
                    defaults=SyncCommand()._variant_defaults(payload, SOURCE_BUILD),
                )
                created += int(added)
            for row, payload in refresh:
                # 保留原 ID、槽位、来源和升级标识，只替换本次核实的数值及其来源。
                WowItemVariantSnapshot.objects.filter(pk=row.pk).update(
                    stats_json=payload['stats'], effects_json=payload['effects'],
                    metadata=payload['metadata'], game_build='',
                )
            if targets:
                current.gear_sync_report = deepcopy(current.gear_sync_report or {})
                current.gear_sync_report['venomstone'] = {'build': SOURCE_BUILD, 'variants': len(targets), 'refreshed_variants': len(refresh), 'levels': [328, 340], 'tooltip_branches': branch_counts}
                current.save(update_fields=['gear_sync_report'])
        report['created'] = created
        self.stdout.write(json.dumps(report, ensure_ascii=False))
        self.stdout.write(self.style.SUCCESS('毒液石选项及相关预览装备各档位数值已更新；原变体标识、当前批次和打孔规则保持不变。'))
