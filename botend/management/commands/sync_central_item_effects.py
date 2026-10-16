"""将现有装备特效归并为公共文本与变体参数，默认仅预检。"""
import json
from django.core.management.base import BaseCommand
from botend.services.wow_item_effects import sync_central_effects, notify_effects_changed


class Command(BaseCommand):
    help = '归并中央装备特效；默认预检，不抓取外网，不修改原属性或历史模拟'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='应用归并结果')
        parser.add_argument('--item-id', type=int, action='append', help='仅处理指定物品，可重复')

    def handle(self, *args, **options):
        from botend.models import WowItemSnapshot
        from botend.services.gear_builder import active_season
        season = active_season()
        report = {'items': 0, 'variants': 0, 'untranslated': 0}
        if season:
            items = WowItemSnapshot.objects.filter(gear_variants__season=season,
                gear_variants__batch_key=season.gear_batch_key).order_by('item_id').values_list('item_id', flat=True).distinct()
            if options['item_id']:
                items = items.filter(item_id__in=options['item_id'])
            ids = list(items)
            # 按物品分块；每个物品的所有难度和语言仍在同一次归并中。
            for offset in range(0, len(ids), 200):
                changed = sync_central_effects(season=season, item_ids=ids[offset:offset + 200],
                    apply=options['apply'], notify=False)
                for key in report:
                    report[key] += changed[key]
            if options['apply'] and (report['items'] or report['variants']):
                notify_effects_changed()
        self.stdout.write(json.dumps({'已应用': options['apply'], '变化': report}, ensure_ascii=False))
