# -*- coding: utf-8 -*-
"""Fetch SpellEffect snapshots for spell ids used by talent descriptions."""

from __future__ import annotations

import html
import csv
import io
import json
import re
import time

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import WowSpellEffectSnapshot, WowTalentNodeMetadata, WowTalentVersion

_REF_RE = re.compile(r"\$(\d+)(?:[A-Za-z])", re.IGNORECASE)


from botend.management.central_talent_command import CentralTalentCommand


class Command(CentralTalentCommand):
    help = "按 spell_id 从 wago.tools 抓 SpellEffect，写入 WowSpellEffectSnapshot"

    def add_arguments(self, parser):
        parser.add_argument('--branch', choices=('retail', 'ptr'), default='retail')
        parser.add_argument('--spell-id', action='append', type=int, default=[], help='指定 spell id，可重复')
        parser.add_argument('--from-talents', action='store_true', help='从含占位符天赋描述中收集 spell id')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--sleep', type=float, default=0.05)
        parser.add_argument('--locale', default='zhCN')
        parser.add_argument('--from-csv', action='store_true', help='下载 SpellEffect CSV 后本地过滤，适合全量天赋相关 spell')
        parser.add_argument(
            '--build',
            default='',
            help='wago.tools build，例如 12.0.7.68235；默认使用所选分支的当前来源构建号',
        )
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from botend.wow.talents.versioning import TalentVersionResolver
        from botend.wow.branches import spell_branch
        self.version = TalentVersionResolver.resolve(version_key=opts['branch'])
        self.branch = spell_branch(opts['branch'])
        ids = set(int(x) for x in (opts.get('spell_id') or []) if x)
        if opts.get('from_talents'):
            talent_spell_ids = set()
            qs = WowTalentNodeMetadata.objects.filter(talent_version=self.version).exclude(spell_id__isnull=True).only('spell_id', 'display_spell_id')
            for row in qs.iterator(chunk_size=500):
                sid = int(row.display_spell_id or row.spell_id or 0)
                if sid:
                    talent_spell_ids.add(sid)
                    ids.add(sid)
            if talent_spell_ids:
                from botend.models import WowSpellSnapshot

                snap_qs = WowSpellSnapshot.objects.filter(branch=self.branch, spell_id__in=talent_spell_ids).only('spell_id', 'description', 'aura_description')
                for snap in snap_qs.iterator(chunk_size=500):
                    text = f"{snap.description or ''}\n{snap.aura_description or ''}"
                    for m in _REF_RE.finditer(text):
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
        build = self._resolve_build(opts.get('build') or '')
        now = timezone.now()
        if opts.get('from_csv'):
            written = self._fetch_from_csv(session, ids, locale, build, now)
            self.stdout.write(self.style.SUCCESS(f'完成 spell={len(ids)} written={written}'))
            return

        total_rows = 0
        written = 0
        for i, sid in enumerate(ids, 1):
            rows = self._fetch_rows(session, sid, locale, build)
            total_rows += len(rows)
            by_index = {}
            for row in rows:
                effect_index = _to_int(row.get('EffectIndex'))
                by_index[effect_index] = WowSpellEffectSnapshot(
                    branch=self.branch,
                    locale=locale,
                    spell_id=sid,
                    effect_index=effect_index,
                    effect=_to_int_or_none(row.get('Effect')),
                    effect_aura=_to_int_or_none(row.get('EffectAura')),
                    misc_value=_to_int_or_none(row.get('EffectMiscValue_0')),
                    base_points=str(row.get('EffectBasePointsF') if row.get('EffectBasePointsF') is not None else row.get('EffectBasePoints') or ''),
                    coefficient=str(row.get('EffectBonusCoefficient') or row.get('BonusCoefficientFromAP') or row.get('Coefficient') or ''),
                    pvp_multiplier=str(row.get('PvpMultiplier') or ''),
                    snapshot_build=build,
                    updated_at=now,
                )
            objs = list(by_index.values())
            if objs:
                written += self._write_effect_rows(objs)
            if i % 50 == 0:
                self.stdout.write(f'  {i}/{len(ids)} effects={written}')
            if float(opts.get('sleep') or 0) > 0:
                time.sleep(float(opts.get('sleep') or 0))
        self.stdout.write(self.style.SUCCESS(f'完成 spell={len(ids)} rows={total_rows} written={written}'))

    def _fetch_from_csv(self, session, ids: list[int], locale: str, build: str, now) -> int:
        id_set = set(ids)
        url = f'https://wago.tools/db2/SpellEffect/csv?build={build}&locale={locale}'
        self.stdout.write(f'下载 SpellEffect CSV: {url}')
        r = session.get(url, timeout=180)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.content.decode('utf-8-sig')))
        by_key = {}
        for row in reader:
            sid = _to_int(row.get('SpellID'))
            if sid not in id_set:
                continue
            effect_index = _to_int(row.get('EffectIndex'))
            by_key[(sid, effect_index)] = WowSpellEffectSnapshot(
                branch=self.branch,
                locale=locale,
                spell_id=sid,
                effect_index=effect_index,
                effect=_to_int_or_none(row.get('Effect')),
                effect_aura=_to_int_or_none(row.get('EffectAura')),
                misc_value=_to_int_or_none(row.get('EffectMiscValue_0')),
                base_points=str(row.get('EffectBasePointsF') if row.get('EffectBasePointsF') not in (None, '') else row.get('EffectBasePoints') or ''),
                coefficient=str(row.get('EffectBonusCoefficient') or row.get('BonusCoefficientFromAP') or row.get('Coefficient') or ''),
                pvp_multiplier=str(row.get('PvpMultiplier') or ''),
                snapshot_build=build,
                updated_at=now,
            )
        objs = list(by_key.values())
        if not objs:
            return 0
        return self._write_effect_rows(objs)

    @transaction.atomic
    def _write_effect_rows(self, rows):
        fields = ('effect', 'effect_aura', 'misc_value', 'base_points', 'coefficient', 'pvp_multiplier', 'snapshot_build')
        grouped = {}
        for row in rows:
            grouped.setdefault((row.branch, row.locale, row.spell_id), []).append(row)
        written = 0
        for (branch, locale, sid), candidates in grouped.items():
            query = WowSpellEffectSnapshot.objects.filter(branch=branch, locale=locale, spell_id=sid)
            existing = {row.effect_index: row for row in query.select_for_update()}
            for row in candidates:
                old = existing.pop(row.effect_index, None)
                if old is None:
                    row.save(force_insert=True)
                    written += 1
                else:
                    changed = [field for field in fields if getattr(old, field) != getattr(row, field)]
                    if changed:
                        for field in changed:
                            setattr(old, field, getattr(row, field))
                        old.updated_at = row.updated_at
                        old.save(update_fields=[*changed, 'updated_at'])
                        written += 1
            if existing:
                query.filter(pk__in=[row.pk for row in existing.values()]).delete()
        return written

    def _resolve_build(self, build):
        build = str(build or '').strip() or getattr(self.version, 'current_build', '')
        if not build:
            raise CommandError('当前分支没有来源构建号，无法选择上游采集源')
        return build

    def _fetch_rows(self, session, spell_id: int, locale: str, build: str) -> list[dict]:
        url = f'https://wago.tools/db2/SpellEffect?build={build}&locale={locale}&filter[SpellID]={spell_id}'
        for attempt in range(4):
            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
                m = re.search(r'data-page=(?:"([^"]+)"|\'([^\']+)\')', r.text or '', re.S)
                if not m:
                    return []
                obj = json.loads(html.unescape(m.group(1) or m.group(2)))
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
