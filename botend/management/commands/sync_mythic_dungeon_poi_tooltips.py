"""为 MDT 交互物品兴趣点同步 Wowhead 中英文 Tooltip。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.mythic_planner.mdt_converter import write_payload
from botend.mythic_planner.wowhead_tooltips import fetch_wowhead_tooltip


class Command(BaseCommand):
    help = '同步 MDT genericItem 点位的 Wowhead 中英文名称和完整说明。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            default='',
            help='要更新的数据包；默认使用 mdt_6_2_5.json。',
        )
        parser.add_argument('--data-env', type=int, default=1)
        parser.add_argument('--difficulty-id', type=int, default=8)
        parser.add_argument('--workers', type=int, default=4)
        parser.add_argument('--delay', type=float, default=0.05)
        parser.add_argument(
            '--force',
            action='store_true',
            help='忽略数据包中已有的完整双语 Tooltip，重新抓取。',
        )

    def handle(self, *args, **options):
        package_path = Path(options['file'] or (
            Path(settings.BASE_DIR)
            / 'botend/data/mythic_planner/mdt_6_2_5.json'
        )).resolve()
        if not package_path.is_file():
            raise CommandError(f'MDT 数据包不存在：{package_path}')

        import json

        try:
            payload = json.loads(package_path.read_text(encoding='utf-8-sig'))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'无法读取 MDT 数据包：{exc}') from exc

        points_by_spell = {}
        for dungeon in payload.get('dungeons') or []:
            for floor in dungeon.get('floors') or []:
                for poi in floor.get('pois') or []:
                    if poi.get('type') != 'genericItem':
                        continue
                    metadata = poi.get('metadata') or {}
                    source = metadata.get('source') or {}
                    info = source.get('info') or {}
                    try:
                        spell_id = int(info.get('spellId') or 0)
                    except (TypeError, ValueError):
                        spell_id = 0
                    if spell_id > 0:
                        points_by_spell.setdefault(spell_id, []).append(poi)

        if not points_by_spell:
            raise CommandError('数据包中没有带 spellId 的 genericItem 点位。')

        pending = []
        for spell_id, points in points_by_spell.items():
            tooltip = (points[0].get('metadata') or {}).get('tooltip') or {}
            complete = bool(
                tooltip.get('name_zh')
                and tooltip.get('name')
                and tooltip.get('description_zh')
                and tooltip.get('description')
            )
            if options['force'] or not complete:
                pending.append(spell_id)

        results = {}
        workers = max(1, int(options['workers']))
        self.stdout.write(
            f'开始同步交互物品 Tooltip：技能 {len(pending)}/{len(points_by_spell)} 个，'
            f'请求 {len(pending) * 2} 次，并发 {workers}。'
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    fetch_wowhead_tooltip,
                    spell_id,
                    locale=locale,
                    data_env=options['data_env'],
                    difficulty_id=options['difficulty_id'],
                    delay=max(0, float(options['delay'])),
                ): (spell_id, locale)
                for spell_id in pending
                for locale in (4, 0)
            }
            for index, future in enumerate(as_completed(futures), start=1):
                spell_id, locale = futures[future]
                results.setdefault(spell_id, {})[locale] = future.result()
                if index % 10 == 0 or index == len(futures):
                    self.stdout.write(
                        f'Wowhead Tooltip 进度：{index}/{len(futures)}。'
                    )

        updated = 0
        failed = []
        for spell_id in pending:
            localized = results.get(spell_id) or {}
            zh = localized.get(4) or {}
            en = localized.get(0) or {}
            if not (zh.get('description') or en.get('description')):
                failed.append(spell_id)
                continue
            tooltip = {
                'name': en.get('name') or zh.get('name') or '',
                'name_zh': zh.get('name') or en.get('name') or '',
                'description': en.get('description') or '',
                'description_zh': zh.get('description') or en.get('description') or '',
                'icon_name': zh.get('icon_name') or en.get('icon_name') or '',
                'source': 'wowhead_tooltip',
                'data_env': int(options['data_env']),
                'difficulty_id': int(options['difficulty_id']),
                'locales': ['enUS', 'zhCN'],
            }
            for poi in points_by_spell[spell_id]:
                metadata = dict(poi.get('metadata') or {})
                metadata['tooltip'] = tooltip
                poi['metadata'] = metadata
                poi['label'] = tooltip['name_zh'] or poi.get('label') or ''
                updated += 1

        write_payload(payload, package_path)
        if failed:
            self.stderr.write(self.style.WARNING(
                f'以下技能未取得可用说明：{", ".join(map(str, failed))}'
            ))
        self.stdout.write(self.style.SUCCESS(
            f'交互物品 Tooltip 同步完成：技能 {len(pending)}/{len(points_by_spell)} 个，'
            f'更新点位 {updated} 个，失败 {len(failed)} 个。'
        ))
        if failed:
            raise CommandError('部分交互物品 Tooltip 同步失败，数据包未写入空说明。')
