"""审计或分离活动配装目录的历史描述与特效，保留原文且不改属性。"""
from copy import deepcopy
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import active_season
from botend.services.wow_item_text import normalize_catalog_text


class Command(BaseCommand):
    help = '默认只审计；指定 --apply 后分离活动目录的描述与特效，原文保留在 metadata 中。'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='写入规范化文本，不修改物品属性和装等')

    def handle(self, *args, **options):
        season = active_season()
        if not season:
            self.stdout.write('没有活动配装目录。')
            return
        items = WowItemSnapshot.objects.filter(gear_variants__season=season, gear_variants__batch_key=season.gear_batch_key).distinct()
        changed_items = changed_variants = 0
        for item in items.iterator():
            with transaction.atomic():
                if options['apply']:
                    item = WowItemSnapshot.objects.select_for_update().get(pk=item.pk)
                variants = list(WowItemVariantSnapshot.objects.filter(item=item, season=season, batch_key=season.gear_batch_key))
                payload = {'name': item.name, 'name_zh': item.name_zh, 'description': item.description,
                    'description_zh': item.description_zh, 'catalog_type': item.catalog_type,
                    'metadata': deepcopy(item.metadata or {}),
                    'variants': [{'stats': row.stats_json, 'effects': deepcopy(row.effects_json),
                                  'metadata': deepcopy(row.metadata or {})} for row in variants]}
                normalize_catalog_text(payload)
                fields = [field for field in ('description', 'description_zh', 'metadata') if getattr(item, field) != payload[field]]
                if fields:
                    changed_items += 1
                    if options['apply']:
                        for field in fields:
                            setattr(item, field, payload[field])
                        item.save(update_fields=fields)
                for row, normalized in zip(variants, payload['variants']):
                    if row.effects_json == normalized['effects'] and row.metadata == normalized['metadata']:
                        continue
                    changed_variants += 1
                    if options['apply']:
                        row.effects_json, row.metadata = normalized['effects'], normalized['metadata']
                        row.save(update_fields=['effects_json', 'metadata'])
        self.stdout.write(json.dumps({'模式': '已写入' if options['apply'] else '仅审计',
            '物品数': changed_items, '变体数': changed_variants}, ensure_ascii=False))
