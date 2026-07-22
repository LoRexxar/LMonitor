# -*- coding: utf-8 -*-

import csv
import os
import tempfile
from dataclasses import dataclass

import requests

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import WowSpellSnapshot, WowSpellSnapshotState


@dataclass
class SyncSummary:
    paired: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    en_only: int = 0
    zh_only: int = 0


class Command(BaseCommand):
    help = '按 spell_id 配对正式服 Wago SpellName enUS/zhCN，并同步双语技能快照'

    def add_arguments(self, parser):
        parser.add_argument('--dump-dir', default='', help='已有 CSV 目录；不传则按 build 从 wago.tools 下载')
        parser.add_argument('--build', required=True, help='两份正式服 CSV 对应的明确 WoW build')
        parser.add_argument('--branch', default='wow', help='只允许正式服 wow')
        parser.add_argument('--batch-size', type=int, default=2000, help='批量写入条数')
        parser.add_argument('--min-paired', type=int, default=1000, help='最少完整双语配对数')
        parser.add_argument('--dry-run', action='store_true', help='只校验和统计，不写数据库')

    def handle(self, *args, **options):
        branch = str(options.get('branch') or '').strip()
        build = str(options.get('build') or '').strip()
        configured_dump_dir = str(options.get('dump_dir') or '').strip()
        batch_size = max(1, int(options.get('batch_size') or 2000))
        min_paired = max(1, int(options.get('min_paired') or 1000))

        if branch != 'wow':
            raise CommandError('branch 只允许正式服 wow，禁止导入 wowt/PTR')
        if not build or build.lower() == 'latest':
            raise CommandError('build 必须是明确的正式服版本，不能使用 latest')

        with tempfile.TemporaryDirectory(prefix='lmonitor-wago-spell-names-') as temporary_dir:
            if configured_dump_dir:
                dump_dir = os.path.abspath(configured_dump_dir)
                if not os.path.isdir(dump_dir):
                    raise CommandError(f'dump 目录不存在: {dump_dir}')
            else:
                dump_dir = temporary_dir
                self._download_names(build, dump_dir)
            return self._sync_from_dump(
                dump_dir=dump_dir,
                build=build,
                batch_size=batch_size,
                min_paired=min_paired,
                dry_run=bool(options.get('dry_run')),
            )

    def _sync_from_dump(self, dump_dir, build, batch_size, min_paired, dry_run):
        names_en = self._load_names(os.path.join(dump_dir, 'SpellName_enUS.csv'), 'enUS')
        names_zh = self._load_names(os.path.join(dump_dir, 'SpellName_zhCN.csv'), 'zhCN')
        paired_ids = sorted(set(names_en) & set(names_zh))
        if len(paired_ids) < min_paired:
            raise CommandError(
                f'完整双语配对不足: paired={len(paired_ids)}, min_paired={min_paired}')

        summary = SyncSummary(
            paired=len(paired_ids),
            en_only=len(set(names_en) - set(names_zh)),
            zh_only=len(set(names_zh) - set(names_en)),
        )
        # Reading the locale once is substantially cheaper than a 400k-value
        # ``spell_id__in`` predicate on MySQL, while the dictionary still limits
        # writes to the paired IDs below.
        is_mysql = settings.DATABASES['default']['ENGINE'] == 'django.db.backends.mysql'
        existing_rows = WowSpellSnapshot.objects.filter(branch='wow', locale='zhCN')
        if is_mysql:
            existing = {
                row['spell_id']: row for row in existing_rows.values(
                    'spell_id', 'name', 'name_zh', 'snapshot_build')
            }
        else:
            existing = {row.spell_id: row for row in existing_rows}
        now = timezone.now()
        creates = []
        updates = []
        for spell_id in paired_ids:
            name = names_en[spell_id]
            name_zh = names_zh[spell_id]
            row = existing.get(spell_id)
            if row is None:
                creates.append(WowSpellSnapshot(
                    branch='wow', locale='zhCN', spell_id=spell_id,
                    name=name, name_zh=name_zh, snapshot_build=build, updated_at=now,
                ))
                summary.created += 1
                continue
            old_name = row['name'] if is_mysql else row.name
            old_name_zh = row['name_zh'] if is_mysql else row.name_zh
            old_build = row['snapshot_build'] if is_mysql else row.snapshot_build
            if old_name == name and old_name_zh == name_zh and old_build == build:
                summary.unchanged += 1
                continue
            if is_mysql:
                row = WowSpellSnapshot(
                    branch='wow', locale='zhCN', spell_id=spell_id,
                    name=name, name_zh=name_zh, snapshot_build=build, updated_at=now,
                )
            else:
                row.name = name
                row.name_zh = name_zh
                row.snapshot_build = build
                row.updated_at = now
            updates.append(row)
            summary.updated += 1

        if not dry_run:
            with transaction.atomic():
                if settings.DATABASES['default']['ENGINE'] == 'django.db.backends.mysql':
                    self._mysql_upsert(creates + updates, batch_size)
                else:
                    if creates:
                        WowSpellSnapshot.objects.bulk_create(creates, batch_size=batch_size)
                    if updates:
                        WowSpellSnapshot.objects.bulk_update(
                            updates, ['name', 'name_zh', 'snapshot_build', 'updated_at'],
                            batch_size=batch_size,
                        )
                WowSpellSnapshotState.objects.update_or_create(
                    branch='wow', locale='zhCN',
                    defaults={'snapshot_build': build},
                )

        self.stdout.write(self.style.SUCCESS(
            f'Wago SpellName 同步完成: build={build}, paired={summary.paired}, '
            f'created={summary.created}, updated={summary.updated}, '
            f'unchanged={summary.unchanged}, en_only={summary.en_only}, '
            f'zh_only={summary.zh_only}, dry_run={dry_run}'
        ))

    @staticmethod
    def _mysql_load_row(row):
        columns = (
            'branch', 'locale', 'spell_id', 'name', 'name_zh',
            'description', 'aura_description', 'snapshot_build', 'updated_at',
        )
        updated_at = row.updated_at or timezone.now()
        values = (
            row.branch, row.locale, row.spell_id, row.name, row.name_zh,
            row.description, row.aura_description, row.snapshot_build,
            updated_at.strftime('%Y-%m-%d %H:%M:%S.%f'),
        )
        return columns, values

    @staticmethod
    def _mysql_upsert(rows, batch_size):
        """Write the live-name snapshot with a bounded MySQL-native upsert.

        ``LOAD DATA LOCAL`` is commonly disabled on managed MySQL. A raw
        multi-row upsert avoids Django's expensive object SQL rendering while
        preserving non-name fields on existing rows.
        """
        if not rows:
            return
        connection = transaction.get_connection()
        table = connection.ops.quote_name(WowSpellSnapshot._meta.db_table)
        columns, _values = Command._mysql_load_row(rows[0])
        quoted_columns = ', '.join(connection.ops.quote_name(column) for column in columns)
        placeholders = ', '.join(['%s'] * len(columns))
        chunk_size = max(1, min(int(batch_size or 2000), 5000))
        update_sql = (
            " ON DUPLICATE KEY UPDATE "
            "`name`=VALUES(`name`), `name_zh`=VALUES(`name_zh`), "
            "`snapshot_build`=VALUES(`snapshot_build`), `updated_at`=VALUES(`updated_at`)"
        )
        with connection.cursor() as cursor:
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                values_clause = ', '.join([f"({placeholders})"] * len(chunk))
                params = []
                for row in chunk:
                    params.extend(Command._mysql_load_row(row)[1])
                cursor.execute(
                    f"INSERT INTO {table} ({quoted_columns}) VALUES {values_clause}{update_sql}",
                    params,
                )

    @staticmethod
    def _download_names(build, dump_dir):
        for locale in ('enUS', 'zhCN'):
            url = f'https://wago.tools/db2/SpellName/csv?build={build}&locale={locale}'
            try:
                response = requests.get(url, timeout=120)
                response.raise_for_status()
            except requests.RequestException as exc:
                raise CommandError(f'下载 {locale} SpellName CSV 失败: {exc}') from exc
            content_type = str(response.headers.get('Content-Type') or '').lower()
            if 'text/csv' not in content_type:
                raise CommandError(
                    f'下载 {locale} SpellName 返回非 CSV 内容: {content_type or "unknown"}')
            path = os.path.join(dump_dir, f'SpellName_{locale}.csv')
            try:
                with open(path, 'wb') as handle:
                    handle.write(response.content)
            except OSError as exc:
                raise CommandError(f'保存 {locale} SpellName CSV 失败: {exc}') from exc

    @staticmethod
    def _load_names(path, locale):
        if not os.path.isfile(path):
            raise CommandError(f'缺少 {locale} SpellName CSV: {path}')
        names = {}
        try:
            with open(path, encoding='utf-8-sig', newline='') as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or not {'ID', 'Name_lang'}.issubset(reader.fieldnames):
                    raise CommandError(f'{locale} CSV 表头必须包含 ID,Name_lang')
                for line_no, raw in enumerate(reader, start=2):
                    try:
                        spell_id = int(raw.get('ID') or 0)
                    except (TypeError, ValueError):
                        continue
                    name = str(raw.get('Name_lang') or '').strip()
                    if spell_id <= 0 or not name:
                        continue
                    if len(name) > 255:
                        raise CommandError(f'{locale} CSV 第 {line_no} 行名称超过 255 字符')
                    previous = names.get(spell_id)
                    if previous is not None and previous != name:
                        raise CommandError(f'{locale} CSV spell_id={spell_id} 存在冲突名称')
                    names[spell_id] = name
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CommandError(f'读取 {locale} SpellName CSV 失败: {exc}') from exc
        if not names:
            raise CommandError(f'{locale} SpellName CSV 没有有效数据')
        return names
