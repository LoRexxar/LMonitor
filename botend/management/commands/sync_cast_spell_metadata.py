"""Synchronize authoritative spell display metadata used by SimC cast timelines."""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.html import strip_tags
from django.utils import timezone

from botend.interface.ossupload import ossUploadObject
from botend.models import WowSpellSnapshot


WOWHEAD_TOOLTIP_URL = 'https://nether.wowhead.com/tooltip/spell/{spell_id}?dataEnv=1&locale={locale}'
WOWHEAD_ICON_URL = 'https://wow.zamimg.com/images/wow/icons/small/{icon}.jpg'


class Command(BaseCommand):
    help = '按指定 SpellID 同步施法时间轴所需双语快照，并补齐 small 图标至 OSS'

    def add_arguments(self, parser):
        parser.add_argument('--spell-ids', required=True, help='逗号分隔的权威 SpellID')
        parser.add_argument('--workers', type=int, default=6)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        try:
            spell_ids = sorted({int(value.strip()) for value in options['spell_ids'].split(',') if value.strip()})
        except ValueError as exc:
            raise CommandError('--spell-ids 必须全部是整数') from exc
        if not spell_ids or any(value <= 0 for value in spell_ids):
            raise CommandError('--spell-ids 必须包含正整数')

        workers = max(1, min(int(options['workers'] or 1), 12))
        dry_run = bool(options['dry_run'])
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._fetch, spell_id): spell_id for spell_id in spell_ids}
            records = []
            for future in as_completed(futures):
                spell_id = futures[future]
                try:
                    records.append(future.result())
                except Exception as exc:
                    self.stderr.write(f'SpellID {spell_id} 同步失败: {exc}')

        synced = icons = 0
        for record in sorted(records, key=lambda item: item['spell_id']):
            spell_id = record['spell_id']
            icon = record['en']['icon'] or record['zh']['icon']
            if not dry_run:
                for locale, payload in (('enUS', record['en']), ('zhCN', record['zh'])):
                    WowSpellSnapshot.objects.update_or_create(
                        branch='wow', locale=locale, spell_id=spell_id,
                        defaults={
                            'name': payload['name'],
                            'name_zh': record['zh']['name'] if locale == 'zhCN' else '',
                            'description': payload['description'],
                            'aura_description': '',
                            'icon': icon,
                            'snapshot_build': 'wowhead-tooltip',
                            'updated_at': timezone.now(),
                        },
                    )
                    synced += 1
                if icon and self._ensure_small_icon(icon):
                    icons += 1
            self.stdout.write(f'SpellID {spell_id}: {record["zh"]["name"] or record["en"]["name"]} · {icon or "无图标"}')
        self.stdout.write(self.style.SUCCESS(
            f'完成: SpellID {len(records)}/{len(spell_ids)}，快照 {synced} 条，确认/上传 small 图标 {icons} 个'
        ))

    @staticmethod
    def _fetch_one(spell_id, locale):
        response = requests.get(WOWHEAD_TOOLTIP_URL.format(spell_id=spell_id, locale=locale), timeout=20)
        response.raise_for_status()
        payload = response.json()
        return {
            'name': str(payload.get('name') or '').strip(),
            'description': ' '.join(strip_tags(str(payload.get('tooltip') or '')).split()),
            'icon': str(payload.get('icon') or '').strip().lower(),
        }

    def _fetch(self, spell_id):
        return {'spell_id': spell_id, 'en': self._fetch_one(spell_id, 0), 'zh': self._fetch_one(spell_id, 4)}

    @staticmethod
    def _ensure_small_icon(icon):
        safe_icon = ''.join(character for character in icon if character.isalnum() or character in '_-')
        if not safe_icon or safe_icon != icon:
            return False
        path = os.path.join(settings.BASE_DIR, 'static', 'wow_icons', 'small', f'{safe_icon}.jpg')
        if not os.path.exists(path):
            response = requests.get(WOWHEAD_ICON_URL.format(icon=safe_icon), timeout=20)
            response.raise_for_status()
            if not response.content.startswith(b'\xff\xd8\xff'):
                raise CommandError(f'图标不是 JPEG: {safe_icon}')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as handle:
                handle.write(response.content)
        return bool(ossUploadObject(path, object_key=f'wow_icons_oss/small/{safe_icon}.jpg'))
