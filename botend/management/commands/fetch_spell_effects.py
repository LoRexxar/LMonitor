# -*- coding: utf-8 -*-
"""Fetch SpellEffect snapshots for spell ids used by talent descriptions."""

from __future__ import annotations

import html
import json
import re
import time

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.models import WowSpellEffectSnapshot, WowTalentNodeMetadata

_REF_RE = re.compile(r"\$(\d+)(?:[A-Za-z])", re.IGNORECASE)


class Command(BaseCommand):
    help = "按 spell_id 从 wago.tools 抓 SpellEffect，写入 WowSpellEffectSnapshot"

    def add_arguments(self, parser):
        parser.add_argument('--spell-id', action='append', type=int, default=[], help='指定 spell id，可重复')
        parser.add_argument('--from-talents', action='store_true', help='从含占位符天赋描述中收集 spell id')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--sleep', type=float, default=0.05)
        parser.add_argument('--locale', default='zhCN')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        ids = set(int(x) for x in (opts.get('spell_id') or []) if x)
        if opts.get('from_talents'):
            qs = WowTalentNodeMetadata.objects.filter(description_zh__contains='$').only('spell_id', 'display_spell_id', 'description_zh')
            for row in qs.iterator(chunk_size=500):
                sid = int(row.display_spell_id or row.spell_id or 0)
                if sid:
                    ids.add(sid)
                for m in _REF_RE.finditer(row.description_zh or ''):
                    try:
                        ids.add(int(m.group(1)))
                    except Exception:
                        pass
        ids = sorted(ids)
        limit = int(opts.get('limit') or 0)
        if limit > 0:
            ids = ids[:limit]
        if not ids:
            self.stdout.write('没有 spell id')
            return

        self.stdout.write(f'准备抓取 {len(ids)} 个 spell 的 SpellEffect')
        if opts.get('dry_run'):
            self.stdout.write(', '.join(map(str, ids[:50])))
            return

        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        locale = opts.get('locale') or 'zhCN'
        now = timezone.now()
        total_rows = 0
        written = 0
        for i, sid in enumerate(ids, 1):
            rows = self._fetch_rows(session, sid, locale)
            total_rows += len(rows)
            by_index = {}
            for row in rows:
                effect_index = _to_int(row.get('EffectIndex'))
                by_index[effect_index] = WowSpellEffectSnapshot(
                    branch='wow',
                    locale=locale,
                    spell_id=sid,
                    effect_index=effect_index,
                    effect=_to_int_or_none(row.get('Effect')),
                    effect_aura=_to_int_or_none(row.get('EffectAura')),
                    base_points=str(row.get('EffectBasePointsF') if row.get('EffectBasePointsF') is not None else row.get('EffectBasePoints') or ''),
                    coefficient=str(row.get('EffectBonusCoefficient') or row.get('BonusCoefficientFromAP') or row.get('Coefficient') or ''),
                    pvp_multiplier=str(row.get('PvpMultiplier') or ''),
                    snapshot_build='',
                    updated_at=now,
                )
            objs = list(by_index.values())
            if objs:
                indexes = [o.effect_index for o in objs]
                WowSpellEffectSnapshot.objects.filter(
                    branch='wow', locale=locale, spell_id=sid, effect_index__in=indexes
                ).delete()
                WowSpellEffectSnapshot.objects.bulk_create(objs, batch_size=100)
                written += len(objs)
            if i % 50 == 0:
                self.stdout.write(f'  {i}/{len(ids)} effects={written}')
            if float(opts.get('sleep') or 0) > 0:
                time.sleep(float(opts.get('sleep') or 0))
        self.stdout.write(self.style.SUCCESS(f'完成 spell={len(ids)} rows={total_rows} written={written}'))

    def _fetch_rows(self, session, spell_id: int, locale: str) -> list[dict]:
        url = f'https://wago.tools/db2/SpellEffect?locale={locale}&filter[SpellID]={spell_id}'
        for attempt in range(4):
            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
                m = re.search(r'data-page="([^"]+)"', r.text or '', re.S)
                if not m:
                    return []
                obj = json.loads(html.unescape(m.group(1)))
                data = (obj.get('props') or {}).get('data') or {}
                return [row for row in (data.get('data') or []) if _to_int(row.get('SpellID')) == spell_id]
            except Exception:
                if attempt >= 3:
                    return []
                time.sleep(2 ** attempt)
        return []


def _to_int(value) -> int:
    try:
        return int(str(value).strip() or '0')
    except Exception:
        return 0


def _to_int_or_none(value):
    try:
        if value is None or value == '':
            return None
        return int(str(value).strip())
    except Exception:
        return None
