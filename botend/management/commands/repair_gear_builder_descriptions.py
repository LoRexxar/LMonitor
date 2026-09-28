"""仅补齐活动装备的中文说明，不重抓或改写属性与特效。"""
from copy import deepcopy
import json
import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from botend.models import WowItemSnapshot
from botend.services.gear_builder import active_season
from botend.services.gear_builder_catalog_source import CurrentGearCatalogSource, CatalogSourceError
from botend.services.wow_item_text import separate_item_text


class Command(BaseCommand):
    help = '审计活动装备的中文说明缺口；加 --apply 才下载中文源并写入说明，保留属性、特效及原文。'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='补抓中文说明并写入数据库')
        parser.add_argument('--item-id', type=int, action='append', help='只处理指定物品，可重复指定')
        parser.add_argument('--cache-dir', default='.cache/gear_builder', help='中文 Tooltip 缓存目录')
        parser.add_argument('--timeout', type=int, default=30, help='单次请求超时秒数')
        parser.add_argument('--no-proxy', action='store_true', help='不使用已配置的代理')

    def handle(self, *args, **options):
        season = active_season()
        if not season:
            self.stdout.write('没有活动配装目录。')
            return
        items = WowItemSnapshot.objects.filter(catalog_type='equipment',
            gear_variants__season=season, gear_variants__batch_key=season.gear_batch_key).distinct()
        if options['item_id']:
            items = items.filter(item_id__in=options['item_id'])
        targets = []
        for item in items:
            text = separate_item_text(description=item.description, description_zh=item.description_zh)
            if not text['description_zh'] and text['description']:
                targets.append(item)
        self.stdout.write(json.dumps({'模式': '补齐中文说明' if options['apply'] else '仅审计',
            '待补齐': [{'物品ID': item.item_id, '名称': item.name_zh or item.name} for item in targets]}, ensure_ascii=False))
        if not options['apply'] or not targets:
            return
        source = CurrentGearCatalogSource(cache_dir=options['cache_dir'], timeout=options['timeout'],
            no_proxy=options['no_proxy'], refresh_wowhead_cache=True)
        cache_dir = source.cache_root / (season.game_build or 'unknown') / 'wowhead'
        cache_dir.mkdir(parents=True, exist_ok=True)
        updated, missing, failed = [], [], []
        for item in targets:
            try:
                # 只读取与装等无关的物品说明，不采用这次下载的属性或效果数值。
                details = source._wowhead_tooltip(item.item_id, 0, cache_dir)
            except CatalogSourceError as exc:
                failed.append({'物品ID': item.item_id, '原因': str(exc)})
                continue
            description = separate_item_text(description_zh=details.get('description_zh') or '')['description_zh']
            if not re.search(r'[\u3400-\u9fff]', description):
                missing.append(item.item_id)
                continue
            with transaction.atomic():
                locked = WowItemSnapshot.objects.select_for_update().get(pk=item.pk)
                current = separate_item_text(description_zh=locked.description_zh)['description_zh']
                if current:
                    continue
                metadata = deepcopy(locked.metadata or {})
                metadata.setdefault('raw_item_descriptions', {
                    'description': locked.description, 'description_zh': locked.description_zh,
                })
                metadata['description_zh_source'] = {
                    'url': f'https://nether.wowhead.com/tooltip/item/{item.item_id}?locale=zhcn',
                    'retrieved_at': timezone.now().isoformat(),
                }
                locked.description_zh = description
                locked.metadata = metadata
                locked.save(update_fields=['description_zh', 'metadata'])
                updated.append(item.item_id)
        self.stdout.write(json.dumps({'已补齐': updated, '源站无中文说明': missing, '请求失败': failed}, ensure_ascii=False))
