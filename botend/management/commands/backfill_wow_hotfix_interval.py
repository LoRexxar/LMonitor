"""Rebuild one verified Wago Hotfix interval without rewinding the live cursor."""

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

    def handle(self, *args, **options):
        from_push, to_push, region_id = (options[key] for key in ('from_push', 'to_push', 'region_id'))
        expected_count = options['expected_count']
        expected_digest = options['expected_id_sha256']
        if from_push <= 0 or to_push <= from_push or region_id <= 0:
            raise CommandError('Specify a positive region and increasing exact push interval')
        if expected_count <= 0 or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
            raise CommandError('Specify independently verified source count and SHA-256')
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
