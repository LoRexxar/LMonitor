"""Rebuild one verified Wago Hotfix interval without rewinding the live cursor."""

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.models import WowHotfixReport, WowWagoMonitorState
from botend.services.wago_hotfix_source import source_ids_sha256


class Command(BaseCommand):
    help = 'Manually rebuild one region-scoped Hotfix interval, preserving the live cursor'

    def add_arguments(self, parser):
        parser.add_argument('--from-push', type=int, required=True)
        parser.add_argument('--to-push', type=int, required=True)
        parser.add_argument('--region-id', type=int, required=True)
        parser.add_argument('--expected-count', type=int, required=True)
        parser.add_argument('--expected-id-sha256', required=True)
        parser.add_argument('--verified-source-json', help='Previously verified Wago rows for only this interval')
        parser.add_argument('--verified-file-sha256', help='SHA-256 of the exact source file, including payloads')

    def handle(self, *args, **options):
        from_push, to_push, region_id = (options[key] for key in ('from_push', 'to_push', 'region_id'))
        expected_count = options['expected_count']
        expected_digest = options['expected_id_sha256']
        if from_push <= 0 or to_push <= from_push or region_id <= 0:
            raise CommandError('Specify a positive region and increasing exact push interval')
        if expected_count <= 0 or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
            raise CommandError('Specify independently verified source count and SHA-256')
        if bool(options.get('verified_source_json')) != bool(options.get('verified_file_sha256')):
            raise CommandError('Verified source file and file SHA-256 must be provided together')
        frozen_rows = None
        if options.get('verified_source_json'):
            path = Path(options['verified_source_json'])
            if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise CommandError('Verified source file missing or oversized')
            file_digest = options.get('verified_file_sha256') or ''
            if not re.fullmatch(r'[0-9a-f]{64}', file_digest):
                raise CommandError('Verified source requires exact file SHA-256')
            try:
                source_bytes = path.read_bytes()
                if hashlib.sha256(source_bytes).hexdigest() != file_digest:
                    raise CommandError('Verified source file SHA-256 mismatch')
                frozen_rows = json.loads(source_bytes.decode('utf-8'))
            except (OSError, ValueError, UnicodeError) as exc:
                raise CommandError('Verified source JSON unreadable') from exc
            if not isinstance(frozen_rows, list) or len(frozen_rows) != expected_count:
                raise CommandError('Verified source identity count mismatch')
            ids, identities = set(), set()
            for row in frozen_rows:
                if not isinstance(row, dict):
                    raise CommandError('Verified source identity contains a non-object row')
                try:
                    sid, push, row_region, record_id = (int(row[key]) for key in ('id', 'push_id', 'region_id', 'record_id'))
                    build, status = int(row['build']), int(row['status'])
                    table = row['table_name']
                    identity = (push, table.lower(), record_id)
                    valid = (sid > 0 and from_push < push <= to_push and row_region == region_id
                             and record_id > 0 and build > 0 and isinstance(table, str)
                             and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', table)
                             and row['locale'] == 'enUS' and (row['data'] is None or isinstance(row['data'], list)))
                except (KeyError, TypeError, ValueError, AttributeError):
                    valid = False
                if not valid or sid in ids or identity in identities:
                    raise CommandError('Verified source identity outside interval, duplicated or malformed')
                ids.add(sid)
                identities.add(identity)
            if source_ids_sha256(frozen_rows) != expected_digest:
                raise CommandError('Verified source identity SHA-256 mismatch')
        try:
            live = WowWagoMonitorState.objects.get(branch='wow', locale='enUS')
        except WowWagoMonitorState.DoesNotExist as exc:
            raise CommandError('WoW enUS Hotfix state does not exist') from exc
        original_cursor = live.hotfix_push_id
        if live.hotfix_region_id != region_id or original_cursor < to_push or not live.build:
            raise CommandError('Region/build are unverified or the requested interval is ahead of the live cursor')
        key = {'branch': 'wow', 'locale': 'enUS', 'region_id': region_id, 'to_push': to_push}
        if WowHotfixReport.objects.filter(**key, collection_complete=True).first():
            raise CommandError('A complete report for this region and target push already exists')

        # _scan_hotfix_if_needed owns the real report/event pipeline. This
        # transient state has no database save method, so a historical replay
        # cannot roll the committed monitor cursor or its latest summary back.
        replay = SimpleNamespace(hotfix_region_id=region_id, hotfix_push_id=from_push,
                                 save=lambda **kwargs: None)
        monitor = WagoSkillDiffMonitor(None, None)
        monitor._hotfix_report_only = True
        if frozen_rows is not None:
            # These are Wago rows, not substitute DB2 facts: the normal
            # decoder/renderer and publication chain still validate them.
            monitor._collect_hotfix_interval_rows = lambda *_args, **_kwargs: frozen_rows
        if not monitor._scan_hotfix_if_needed(
                replay, 'wow', live.build, backfill_interval=(from_push, to_push),
                expected_source_count=expected_count, expected_source_sha256=expected_digest):
            raise CommandError('Hotfix interval generation did not complete; live cursor is unchanged')
        report = WowHotfixReport.objects.filter(**key).first()
        try:
            facts = json.loads(report.source_facts_json) if report else None
        except (TypeError, ValueError):
            facts = None
        if (not report or not report.collection_complete or report.from_push != from_push
                or not isinstance(facts, list) or len(facts) != expected_count
                or report.entry_count != expected_count
                or source_ids_sha256([fact['source'] for fact in facts]) != expected_digest):
            raise CommandError('Hotfix report did not persist complete source facts')
        report_root = Path(settings.BASE_DIR) / 'static'
        paths = [report.content_html_path]
        if report.class_spell_count:
            paths.append(report.class_content_html_path)
        if any(not path or not (report_root / path).is_file() for path in paths):
            raise CommandError('Hotfix report publication files are missing')
        live.refresh_from_db()
        if live.hotfix_region_id != region_id or live.hotfix_push_id < original_cursor:
            raise CommandError('Live Hotfix cursor regressed during manual backfill')
        self.stdout.write(self.style.SUCCESS(
            f'Hotfix report #{report.pk}: region={region_id} push={from_push}→{to_push} '
            f'sources={report.entry_count} class_spells={report.class_spell_count}'
        ))
