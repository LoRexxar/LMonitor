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
WOWHEAD_EN_ITEM = 'https://www.wowhead.com/item={item_id}'
WOWHEAD_TOOLTIP_API = 'https://nether.wowhead.com/tooltip/item/{item_id}?locale={locale}'


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


def _decode_js_string(raw):
    if raw is None:
        return ''
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return html.unescape(str(raw))


def _clean_effect_html(value):
    if not value:
        return ''
    value = _decode_js_string(value) if '\\' in str(value) else str(value)
    value = value.replace('\b', '')
    value = re.sub(r'<!--.*?-->', ' ', value, flags=re.S)
    value = re.sub(r'<br\s*/?>', '\n', value, flags=re.I)
    value = re.sub(r'</(?:div|p|span)>', '\n', value, flags=re.I)
    value = re.sub(r'</(?:table|tr|td|th)>', ' ', value, flags=re.I)
    value = html.unescape(re.sub(r'<[^>]+>', '', value))
    lines = []
    noise_prefixes = (
        '物品等级', '需要等级', '最大叠加', '售价', '拾取后绑定',
        '唯一', '装备唯一', '职业', '耐久度', '需要 ', 'Requires ',
    )
    for raw_line in value.splitlines():
        line = re.sub(r'\s+', ' ', raw_line).strip(' ：:')
        if not line or line in {'使用', '装备', '效果'}:
            continue
        if line.startswith('拾取后绑定'):
            tail = line.replace('拾取后绑定', '', 1).replace('唯一', '').strip(' ：:')
            if tail:
                line = tail
            else:
                continue
        if line.startswith('Binds when picked up'):
            tail = line.replace('Binds when picked up', '', 1).replace('Unique', '').strip(' ：:')
            if tail:
                line = tail
            else:
                continue
        if line.startswith('唯一'):
            tail = line.replace('唯一', '', 1).strip(' ：:')
            if tail:
                line = tail
            else:
                continue
        if line.startswith('Unique'):
            tail = line.replace('Unique', '', 1).strip(' ：:')
            if tail:
                line = tail
            else:
                continue
        if any(line.startswith(prefix) for prefix in noise_prefixes):
            continue
        if re.fullmatch(r'[0-9金银铜 ]+', line):
            continue
        lines.append(line)
    return '\n'.join(dict.fromkeys(lines)).strip()


def _extract_assignment_string(text, pattern):
    match = re.search(pattern, text, re.S)
    return _decode_js_string(match.group(1)) if match else ''


def _extract_item_tooltip(text, item_id, locale='zhcn'):
    tooltip = _extract_assignment_string(
        text,
        rf'g_items\[{int(item_id)}\]\.tooltip_{re.escape(locale)}\s*=\s*"((?:\\.|[^"\\])*)"',
    )
    return _clean_effect_html(tooltip)


def _extract_profession_description(text, locale='zhcn'):
    candidates = []
    has_locale_filter = locale == 'zhcn'
    for raw in re.findall(rf'"description_{re.escape(locale)}"\s*:\s*"((?:\\.|[^"\\])*)"', text, re.S):
        desc = _clean_effect_html(raw)
        if desc and (not has_locale_filter or _has_cjk(desc)):
            candidates.append(desc)
    return max(candidates, key=len) if candidates else ''


def _extract_meta_description(text, require_cjk=True):
    candidates = []
    for pat in [
        r'<meta\s+name="description"\s+content="([^"]+)"',
        r'<meta\s+property="og:description"\s+content="([^"]+)"',
        r'"description"\s*:\s*"((?:\\.|[^"\\])*)"',
    ]:
        for match in re.findall(pat, text, re.S | re.I):
            val = _clean_effect_html(match)
            if val and (not require_cjk or _has_cjk(val)) and not _is_wowhead_seo_description(val):
                candidates.append(val)
    if not candidates:
        return ''
    desc = max(candidates, key=len)
    return re.sub(r'\s*-\s*(?:魔兽世界|World of Warcraft).*$','', desc).strip()


def _strip_description_name(desc, title):
    if not desc or not title:
        return desc or ''
    title = title.strip()
    lines = []
    for line in str(desc).splitlines():
        clean = line.strip()
        if clean and clean != title:
            lines.append(clean)
    return '\n'.join(lines).strip()


def _is_wowhead_seo_description(value):
    value = str(value or '')
    if not value:
        return False
    noise_markers = (
        '添加于 [World of Warcraft',
        'Always up to date with the latest patch',
        '始终保持更新',
        '[In the ',
        '物品放置于',
        '这是295级',
    )
    return any(marker in value for marker in noise_markers)


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
            needs_cn = not (row and _has_cjk(row.name_zh) and _has_cjk(row.description_zh) and not _is_wowhead_seo_description(row.description_zh))
            needs_en = not (row and row.description and not _is_wowhead_seo_description(row.description))
            has_complete_row = row and not needs_cn and not needs_en
            if has_complete_row and not opts['force']:
                skipped += 1
                continue
            meta = self._fetch_wowhead_cn(session, item_id) if (opts['force'] or needs_cn) else {}
            meta_en = self._fetch_wowhead_en(session, item_id) if (opts['force'] or needs_en) else {}
            if not meta and not meta_en:
                failed += 1
                meta = {}
                meta_en = {}
            fallback_name_zh = fallback.get('name_zh') if _has_cjk(fallback.get('name_zh')) else ''
            fallback_desc_zh = fallback.get('description_zh') if _has_cjk(fallback.get('description_zh')) and not _is_wowhead_seo_description(fallback.get('description_zh')) else ''
            row_name_zh = row.name_zh if row and _has_cjk(row.name_zh) else ''
            row_desc_zh = row.description_zh if row and _has_cjk(row.description_zh) and not _is_wowhead_seo_description(row.description_zh) else ''
            row_description = row.description if row and row.description and not _is_wowhead_seo_description(row.description) else ''
            fallback_description = fallback.get('description') if fallback.get('description') and not _is_wowhead_seo_description(fallback.get('description')) else ''
            payload = {
                'name': fallback.get('name') or (row.name if row else '') or meta_en.get('name') or '',
                'name_zh': meta.get('name_zh') or row_name_zh or fallback_name_zh,
                'description': meta_en.get('description') or fallback_description or row_description,
                'description_zh': meta.get('description_zh') or row_desc_zh or fallback_desc_zh,
                'icon': fallback.get('icon') or (row.icon if row else '') or meta.get('icon') or meta_en.get('icon') or '',
                'quality': _coerce_quality(fallback.get('quality') or meta.get('quality') or meta_en.get('quality') or (row.quality if row else 0)),
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

    def _fetch_wowhead_tooltip_api(self, session, item_id, locale):
        url = WOWHEAD_TOOLTIP_API.format(item_id=item_id, locale=locale)
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code >= 400:
                return {}
            data = resp.json()
        except Exception:
            return {}
        title = _first_text(data.get('name') or '')
        desc = _strip_description_name(_clean_effect_html(data.get('tooltip') or ''), title)
        return {
            'name': title,
            'description': desc,
            'icon': _norm_icon(data.get('icon') or ''),
            'quality': _coerce_quality(data.get('quality')),
        }

    def _fetch_wowhead_cn(self, session, item_id):
        api_meta = self._fetch_wowhead_tooltip_api(session, item_id, 'zhCN')
        if api_meta and (_has_cjk(api_meta.get('name')) or _has_cjk(api_meta.get('description'))):
            return {
                'name_zh': api_meta.get('name') if _has_cjk(api_meta.get('name')) else '',
                'description_zh': api_meta.get('description') if _has_cjk(api_meta.get('description')) else '',
                'icon': api_meta.get('icon') or '',
                'quality': api_meta.get('quality') or 0,
            }
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
        desc = _extract_item_tooltip(text, item_id, locale='zhcn') or _extract_profession_description(text, locale='zhcn') or _extract_meta_description(text, require_cjk=True)
        desc = _strip_description_name(desc, title)
        icon = ''
        im = re.search(r'images/wow/icons/(?:small|medium|large)/([a-z0-9_]+)\.(?:jpg|png)', text, re.I)
        if im:
            icon = im.group(1)
        return {
            'name_zh': title if _has_cjk(title) else '',
            'description_zh': desc if _has_cjk(desc) else '',
            'icon': icon,
        }

    def _fetch_wowhead_en(self, session, item_id):
        api_meta = self._fetch_wowhead_tooltip_api(session, item_id, 'enUS')
        if api_meta and (api_meta.get('name') or api_meta.get('description')):
            return api_meta
        url = WOWHEAD_EN_ITEM.format(item_id=item_id)
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
        desc = _extract_item_tooltip(text, item_id, locale='enus') or _extract_profession_description(text, locale='enus') or _extract_meta_description(text, require_cjk=False)
        desc = _strip_description_name(desc, title)
        icon = ''
        im = re.search(r'images/wow/icons/(?:small|medium|large)/([a-z0-9_]+)\.(?:jpg|png)', text, re.I)
        if im:
            icon = im.group(1)
        return {
            'name': title,
            'description': desc,
            'icon': icon,
        }
