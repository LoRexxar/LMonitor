"""从正式服数据源或离线目录同步职业配装器数据。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder_catalog_source import CatalogSourceError, CurrentGearCatalogSource


VALID_TYPES = {value for value, _label in WowItemVariantSnapshot.TYPE_CHOICES}
VALID_TRACKS = {'champion', 'hero', 'myth'}


class Command(BaseCommand):
    help = '自动抓取或导入职业配装器目录，审计通过后可原子切换当前批次。'

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument('--fetch-current', action='store_true', help='自动抓取当前正式服完整目录')
        source.add_argument('--input', default='', help='导入已生成的规范化目录 JSON（离线回放用）')
        parser.add_argument('--season-key', default='', help='覆盖 JSON 中的赛季标识')
        parser.add_argument('--activate', action='store_true', help='审计通过后激活该批次')
        parser.add_argument('--dry-run', action='store_true', help='只解析和审计，不写数据库')
        parser.add_argument('--refresh-wowhead', action='store_true', help='导入物品后调用现有 Wowhead 元数据任务')
        parser.add_argument('--create-season', action='store_true', help='赛季不存在时根据远端数据自动创建')
        parser.add_argument('--skip-wowhead', action='store_true', help='跳过中文 Tooltip/装等属性补全（仅用于排障）')
        parser.add_argument('--workers', type=int, default=8, help='Wowhead 并发数，默认 8，最大 24')
        parser.add_argument('--timeout', type=int, default=45, help='单次远端请求超时秒数')
        parser.add_argument('--cache-dir', default='.cache/gear_builder', help='远端数据缓存目录')
        parser.add_argument('--output', default='', help='可选：把自动生成的规范化目录同时保存为 JSON')
        parser.add_argument('--no-proxy', action='store_true', help='忽略服务端代理环境变量')
        parser.add_argument('--sync-icons', action='store_true', help='写入目录后流式下载并立即上传当前批次图标')
        parser.add_argument('--icon-workers', type=int, default=4, help='图标同步有界并发数，默认 4')
        parser.add_argument('--icon-prefix', default='wow_icons_oss', help='图标 OSS 对象前缀')

    def handle(self, *args, **options):
        if options['fetch_current']:
            try:
                source = CurrentGearCatalogSource(
                    cache_dir=options['cache_dir'], workers=options['workers'], timeout=options['timeout'],
                    no_proxy=options['no_proxy'], progress=self.stdout.write,
                )
                payload = source.build(
                    season_key=str(options.get('season_key') or ''),
                    include_wowhead=not options['skip_wowhead'],
                )
            except CatalogSourceError as exc:
                raise CommandError(str(exc)) from exc
            if options['output']:
                output_path = Path(options['output']).expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                self.stdout.write(f'已保存规范化目录：{output_path}')
        else:
            path = Path(options['input']).expanduser().resolve()
            if not path.is_file():
                raise CommandError(f'目录文件不存在：{path}')
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                raise CommandError(f'目录文件无法解析：{exc}') from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            raise CommandError('目录 JSON 必须包含 items 数组')

        season_key = str(options.get('season_key') or payload.get('season_key') or '').strip()
        batch_key = str(payload.get('batch_key') or '').strip()
        game_build = str(payload.get('game_build') or payload.get('build') or '').strip()
        if not season_key or not batch_key or not game_build:
            raise CommandError('目录必须提供 season_key、batch_key 和 game_build')
        report = self._audit_payload(payload)
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if report['blocking_errors']:
            raise CommandError('目录审计失败，未写入数据库')
        if options['dry_run']:
            self.stdout.write(self.style.SUCCESS('预检通过；dry-run 未写入数据库。'))
            return

        season = SeasonMeta.objects.filter(season_key=season_key).first()
        if not season and options['fetch_current'] and options['create_season']:
            season = self._create_season(payload, activate=options['activate'])
            self.stdout.write(self.style.SUCCESS(f'已创建赛季元数据：{season.season_key}'))
        if not season:
            raise CommandError(f'未找到赛季：{season_key}')

        item_ids = []
        with transaction.atomic():
            for item_payload in payload['items']:
                item = self._upsert_item(item_payload)
                item_ids.append(item.item_id)
                for variant_payload in item_payload.get('variants') or []:
                    defaults = self._variant_defaults(variant_payload, game_build)
                    WowItemVariantSnapshot.objects.update_or_create(
                        season=season,
                        batch_key=batch_key,
                        item=item,
                        variant_key=str(variant_payload.get('key') or variant_payload.get('variant_key') or '').strip(),
                        defaults=defaults,
                    )

        if options['refresh_wowhead'] and item_ids:
            call_command('fetch_item_metadata', item_id=item_ids)

        db_report = self._audit_batch(season, batch_key)
        if db_report['blocking_errors']:
            raise CommandError('数据库批次审计失败，当前赛季未切换')
        if options['sync_icons']:
            call_command(
                'sync_gear_builder_icons',
                season_key=season_key,
                batch_key=batch_key,
                size='medium',
                prefix=options['icon_prefix'],
                workers=options['icon_workers'],
                no_proxy=options['no_proxy'],
                stdout=self.stdout,
                stderr=self.stderr,
            )
        if options['activate']:
            with transaction.atomic():
                locked = SeasonMeta.objects.select_for_update().get(pk=season.pk)
                locked.game_build = game_build
                locked.gear_batch_key = batch_key
                locked.gear_sync_status = 'ready'
                locked.gear_synced_at = timezone.now()
                locked.gear_sync_report = db_report
                locked.save(update_fields=(
                    'game_build', 'gear_batch_key', 'gear_sync_status',
                    'gear_synced_at', 'gear_sync_report', 'updated_at',
                ))
            self.stdout.write(self.style.SUCCESS(f'已激活装备目录批次：{batch_key}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'已写入待激活批次：{batch_key}'))

    @staticmethod
    def _create_season(payload, activate=False):
        info = payload.get('season_info') or {}
        return SeasonMeta.objects.create(
            season_key=str(payload['season_key'])[:30],
            season_name=str(payload.get('season_name') or payload['season_key'])[:100],
            is_active=bool(activate),
            mplus_zone_id=int(info.get('mplus_zone_id') or 0),
            mplus_zone_name=str(info.get('mplus_zone_name') or '')[:100],
            raid_zone_id=int(info.get('raid_zone_id') or 0),
            raid_zone_name=str(info.get('raid_zone_name') or '')[:100],
            raid_zones=info.get('raid_zones') or [],
            mplus_encounters=info.get('mplus_encounters') or [],
            raid_encounters=info.get('raid_encounters') or [],
            delve_sources=info.get('delve_sources') or [],
        )

    def _audit_payload(self, payload):
        counts = Counter()
        errors = []
        warnings = []
        seen = set()
        provider = payload.get('provider') or {}
        provider_text = json.dumps(provider, ensure_ascii=False).casefold()
        if 'wago' not in provider_text:
            warnings.append('provider 未声明 Wago 来源。')
        if 'wowhead' not in provider_text:
            warnings.append('provider 未声明 Wowhead 来源。')
        for item_index, item in enumerate(payload.get('items') or []):
            item_id = item.get('item_id')
            if not isinstance(item_id, int) or item_id <= 0:
                errors.append(f'items[{item_index}] 缺少合法 item_id。')
                continue
            if not item.get('name_zh'):
                warnings.append(f'物品 {item_id} 缺少中文名。')
            for variant_index, variant in enumerate(item.get('variants') or []):
                variant_type = str(variant.get('type') or variant.get('variant_type') or '')
                variant_key = str(variant.get('key') or variant.get('variant_key') or '')
                identity = (item_id, variant_key)
                if not variant_key:
                    errors.append(f'物品 {item_id} 的变体 {variant_index} 缺少 key。')
                elif identity in seen:
                    errors.append(f'物品 {item_id} 的变体键重复：{variant_key}')
                seen.add(identity)
                if variant_type not in VALID_TYPES:
                    errors.append(f'物品 {item_id} 的变体类型无效：{variant_type}')
                    continue
                counts[variant_type] += 1
                if variant_type == WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT:
                    track = str(variant.get('upgrade_track') or '')
                    if track not in VALID_TRACKS:
                        errors.append(f'掉落装备 {item_id}/{variant_key} 不属于勇士、英雄或神话轨道。')
                if variant_type in (WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT):
                    if not (variant.get('compatible_slots') or item.get('slot_key')):
                        errors.append(f'装备 {item_id}/{variant_key} 缺少适用槽位。')
                    if not int(variant.get('item_level') or 0):
                        errors.append(f'装备 {item_id}/{variant_key} 缺少装等。')
                    if not (variant.get('stats') or variant.get('stats_json') or variant.get('effects') or variant.get('effects_json')):
                        warnings.append(f'装备 {item_id}/{variant_key} 缺少属性或特效。')
                if not variant.get('source') and not variant.get('sources'):
                    warnings.append(f'物品 {item_id}/{variant_key} 缺少来源。')
        return {
            'item_count': len(payload.get('items') or []),
            'variant_counts': dict(counts),
            'blocking_errors': errors,
            'warnings': warnings[:200],
        }

    def _upsert_item(self, payload):
        item_id = int(payload['item_id'])
        existing = WowItemSnapshot.objects.filter(item_id=item_id).first()
        defaults = {
            'name': payload.get('name') or getattr(existing, 'name', ''),
            'name_zh': payload.get('name_zh') or getattr(existing, 'name_zh', ''),
            'description': payload.get('description') or getattr(existing, 'description', ''),
            'description_zh': payload.get('description_zh') or getattr(existing, 'description_zh', ''),
            'icon': payload.get('icon') or getattr(existing, 'icon', ''),
            'quality': int(payload.get('quality') or getattr(existing, 'quality', 0) or 0),
            'source': str(payload.get('source') or getattr(existing, 'source', '') or 'wago_wowhead')[:32],
            'catalog_type': payload.get('catalog_type') or '',
            'inventory_type': int(payload.get('inventory_type') or 0),
            'slot_key': payload.get('slot_key') or '',
            'item_class_id': int(payload.get('item_class_id') or 0),
            'item_subclass_id': int(payload.get('item_subclass_id') or 0),
            'armor_type': payload.get('armor_type') or '',
            'weapon_type': payload.get('weapon_type') or '',
            'allowable_class_mask': int(payload.get('allowable_class_mask') or 0),
            'eligible_specs': payload.get('eligible_specs') or [],
            'unique_group': payload.get('unique_group') or '',
            'effect_refs': payload.get('effect_refs') or [],
            'simc_token': payload.get('simc_token') or '',
            'enchantment_id': int(payload.get('enchantment_id') or 0),
            'metadata': payload.get('metadata') or {},
            'updated_at': timezone.now(),
        }
        item, _created = WowItemSnapshot.objects.update_or_create(item_id=item_id, defaults=defaults)
        return item

    def _variant_defaults(self, payload, game_build):
        variant_type = str(payload.get('type') or payload.get('variant_type') or '')
        sources = payload.get('sources')
        if sources is None:
            sources = payload.get('source')
        if sources and not isinstance(sources, list):
            sources = [sources]
        return {
            'game_build': game_build,
            'variant_type': variant_type,
            'item_level': int(payload.get('item_level') or 0),
            'upgrade_track': payload.get('upgrade_track') or '',
            'track_rank': int(payload.get('track_rank') or 0),
            'track_max_rank': int(payload.get('track_max_rank') or 0),
            'crafting_quality': int(payload.get('crafting_quality') or 0),
            'bonus_ids': payload.get('bonus_ids') or [],
            'compatible_slots': payload.get('compatible_slots') or [],
            'socket_types': payload.get('socket_types') or [],
            'socket_count': int(payload.get('socket_count') or 0),
            'stats_json': payload.get('stats') or payload.get('stats_json') or {},
            'effects_json': payload.get('effects') or payload.get('effects_json') or [],
            'source_json': sources or [],
            'crafting_options': payload.get('crafting_options') or {},
            'unique_group': payload.get('unique_group') or '',
            'max_equipped': int(payload.get('max_equipped') or 0),
            'is_intrinsic_embellishment': bool(payload.get('is_intrinsic_embellishment')),
            'metadata': payload.get('metadata') or {},
        }

    def _audit_batch(self, season, batch_key):
        rows = WowItemVariantSnapshot.objects.filter(season=season, batch_key=batch_key).select_related('item')
        counts = Counter(rows.values_list('variant_type', flat=True))
        errors = []
        warnings = []
        missing = {
            'name_zh': [],
            'stats': [],
            'compatible_slots': [],
            'sources': [],
            'effect_mapping': [],
        }
        if not rows.exists():
            errors.append('批次没有任何变体。')
        for row in rows.iterator():
            identity = f'{row.item.item_id}/{row.variant_key}'
            if not row.item.name_zh:
                missing['name_zh'].append(identity)
                warnings.append(f'物品 {row.item.item_id} 缺少中文名。')
            if row.variant_type in (
                WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
                WowItemVariantSnapshot.TYPE_GEM,
                WowItemVariantSnapshot.TYPE_ENCHANT,
            ) and not row.stats_json and not row.effects_json:
                missing['stats'].append(identity)
                warnings.append(f'{identity} 缺少属性或效果。')
            if row.variant_type in (
                WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
            ) and not (row.compatible_slots or row.item.slot_key):
                missing['compatible_slots'].append(identity)
                errors.append(f'{identity} 缺少适用槽位。')
            if not row.source_json:
                missing['sources'].append(identity)
                warnings.append(f'{identity} 缺少来源。')
            if (row.item.effect_refs or (row.metadata or {}).get('requires_effect_mapping')) and not row.effects_json:
                missing['effect_mapping'].append(identity)
                warnings.append(f'{identity} 缺少效果映射。')
        return {
            'item_count': rows.values('item_id').distinct().count(),
            'variant_count': rows.count(),
            'variant_counts': dict(counts),
            'missing_counts': {key: len(value) for key, value in missing.items()},
            'missing': {key: value[:200] for key, value in missing.items()},
            'blocking_errors': errors,
            'warnings': warnings[:200],
        }
