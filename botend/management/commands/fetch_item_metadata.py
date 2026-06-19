# -*- coding: utf-8 -*-
"""抓取装备/宝石/附魔中文元数据。"""

import html
import json
import re
import time
from collections import OrderedDict

import requests
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from botend.models import PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking, WowItemSnapshot


WOWHEAD_CN_ITEM = 'https://www.wowhead.com/cn/item={item_id}'


def _norm_icon(icon):
    icon = str(icon or '').strip().split('?', 1)[0].rsplit('/', 1)[-1]
    for ext in ('.jpg', '.jpeg', '.png', '.webp'):
        if icon.lower().endswith(ext):
            icon = icon[:-len(ext)]
    return icon


QUALITY_TEXT_MAP = {'poor': 0, 'common': 1, 'uncommon': 2, 'rare': 3, 'epic': 4, 'legendary': 5, 'artifact': 6, 'heirloom': 7}


def _coerce_quality(value):
    if value in (None, ''):
        return 0
    if isinstance(value, str):
        value = value.strip().lower()
        if value in QUALITY_TEXT_MAP:
            return QUALITY_TEXT_MAP[value]
    try:
        return int(value)
    except Exception:
        return 0


def _first_text(value):
    if value is None:
        return ''
    value = html.unescape(str(value))
    value = re.sub(r'<[^>]+>', '', value)
    return re.sub(r'\s+', ' ', value).strip()


def _has_cjk(value):
    return bool(re.search(r'[\u3400-\u9fff]', str(value or '')))


def _clean_wowhead_title(title):
    """Wowhead CN title: 破法者的护腕—物品—魔兽世界 / Name - Item - World of Warcraft."""
    title = _first_text(title)
    title = re.sub(r'^\[|\]$', '', title).strip()
    for sep in ('—', ' - '):
        if sep in title:
            title = title.split(sep, 1)[0].strip()
            break
    return re.sub(r'^\[|\]$', '', title).strip()


class Command(BaseCommand):
    help = '从现有 gear_json 收集装备/宝石/附魔 ID，并从 Wowhead CN 抓取中文名称/描述落库。'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0, help='最多抓取多少个物品（0=全部）')
        parser.add_argument('--sleep', type=float, default=0.2, help='请求间隔秒数')
        parser.add_argument('--force', action='store_true', help='强制刷新已有中文数据')
        parser.add_argument('--season-id', type=int, default=0, help='仅收集指定赛季')
        parser.add_argument('--item-id', action='append', type=int, default=[], help='只抓取指定物品 ID；可重复传入')

    def handle(self, *args, **opts):
        item_ids = [item_id for item_id in (opts.get('item_id') or []) if item_id]
        if item_ids:
            # 调试/补单个物品时不要先全表扫描 gear_json。
            items = OrderedDict((item_id, {}) for item_id in item_ids)
        else:
            items = self._collect_items(opts.get('season_id') or None)
        if opts['limit']:
            items = OrderedDict(list(items.items())[:opts['limit']])
        self.stdout.write(f'待处理物品数: {len(items)}')
        if not items:
            return

        existing = {int(r.item_id): r for r in WowItemSnapshot.objects.filter(item_id__in=list(items.keys()))}
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; LMonitor/1.0)'})
        created = updated = skipped = failed = 0
        for idx, (item_id, fallback) in enumerate(items.items(), 1):
            row = existing.get(int(item_id))
            if row and _has_cjk(row.name_zh) and row.description_zh and not opts['force']:
                skipped += 1
                continue
            meta = self._fetch_wowhead_cn(session, item_id)
            if not meta:
                failed += 1
                meta = {}
            fallback_name_zh = fallback.get('name_zh') if _has_cjk(fallback.get('name_zh')) else ''
            row_name_zh = row.name_zh if row and _has_cjk(row.name_zh) else ''
            payload = {
                'name': fallback.get('name') or (row.name if row else '') or meta.get('name') or '',
                'name_zh': meta.get('name_zh') or row_name_zh or fallback_name_zh,
                'description': fallback.get('description') or (row.description if row else '') or '',
                'description_zh': meta.get('description_zh') or (row.description_zh if row and _has_cjk(row.description_zh) else '') or '',
                'icon': fallback.get('icon') or (row.icon if row else '') or meta.get('icon') or '',
                'quality': _coerce_quality(fallback.get('quality') or (row.quality if row else 0)),
                'source': 'wowhead_cn',
                'updated_at': timezone.now(),
            }
            with transaction.atomic():
                obj, was_created = WowItemSnapshot.objects.update_or_create(item_id=item_id, defaults=payload)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
            if idx % 25 == 0:
                self.stdout.write(f'进度 {idx}/{len(items)} created={created} updated={updated} skipped={skipped} failed={failed}')
            time.sleep(opts['sleep'])
        self.stdout.write(self.style.SUCCESS(f'完成 created={created} updated={updated} skipped={skipped} failed={failed}'))

    def _collect_items(self, season_id=None):
        result = OrderedDict()
        querysets = [
            PlayerSpecTopPlayer.objects.all().values('gear_json'),
            SpecDungeonRanking.objects.all().values('gear_json'),
            SpecRaidRanking.objects.all().values('gear_json'),
        ]
        if season_id:
            querysets = [qs.filter(season_id=season_id) for qs in querysets]
        for qs in querysets:
            for row in qs.iterator(chunk_size=200):
                gear = row.get('gear_json') or []
                if isinstance(gear, str):
                    try:
                        gear = json.loads(gear)
                    except Exception:
                        continue
                if not isinstance(gear, list):
                    continue
                for item in gear:
                    if not isinstance(item, dict):
                        continue
                    self._add_item(result, item.get('id') or item.get('itemID') or item.get('item_id'), item)
                    for gem in item.get('gems_detail') or []:
                        if isinstance(gem, dict):
                            self._add_item(result, gem.get('id'), gem)
                    for ench in item.get('enchants_detail') or []:
                        if isinstance(ench, dict):
                            self._add_item(result, ench.get('id'), ench)
        return result

    def _add_item(self, result, item_id, payload):
        try:
            item_id = int(item_id)
        except Exception:
            return
        if item_id <= 0 or item_id in result:
            return
        result[item_id] = {
            'name': payload.get('name') or '',
            'name_zh': payload.get('name_zh') or '',
            'description': payload.get('description') or '',
            'description_zh': payload.get('description_zh') or '',
            'icon': _norm_icon(payload.get('icon') or ''),
            'quality': _coerce_quality(payload.get('quality') or 0),
        }

    def _fetch_wowhead_cn(self, session, item_id):
        url = WOWHEAD_CN_ITEM.format(item_id=item_id)
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code >= 400:
                return {}
            text = resp.text
        except Exception:
            return {}
        title = ''
        m = re.search(r'<title>(.*?)</title>', text, re.S | re.I)
        if m:
            title = _clean_wowhead_title(m.group(1))
        desc = ''
        # Wowhead 的 CN 页面通常把 tooltip 文本嵌在 markup/description/json 字段中，这里尽量提取可读中文句子。
        candidates = []
        for pat in [
            r'<meta\s+name="description"\s+content="([^"]+)"',
            r'<meta\s+property="og:description"\s+content="([^"]+)"',
            r'"description"\s*:\s*"((?:\\.|[^"\\])*)"',
        ]:
            for match in re.findall(pat, text, re.S | re.I):
                try:
                    val = bytes(match, 'utf-8').decode('unicode_escape') if '\\' in match else match
                except Exception:
                    val = match
                val = _first_text(val)
                if val and re.search(r'[\u4e00-\u9fff]', val):
                    candidates.append(val)
        if candidates:
            # 选最长的中文描述，去掉站点噪音。
            desc = max(candidates, key=len)
            desc = re.sub(r'\s*-\s*魔兽世界.*$', '', desc).strip()
        icon = ''
        im = re.search(r'images/wow/icons/(?:small|medium|large)/([a-z0-9_]+)\.(?:jpg|png)', text, re.I)
        if im:
            icon = im.group(1)
        return {
            'name_zh': title if _has_cjk(title) else '',
            'description_zh': desc if _has_cjk(desc) else '',
            'icon': icon,
        }
