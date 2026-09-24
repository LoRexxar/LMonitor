"""Re-render one already complete Hotfix report from saved source facts only."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.models import WowHotfixReport
from botend.services.wago_hotfix_source import source_ids_sha256


class Command(BaseCommand):
    help = 'Re-render an existing verified Hotfix HTML pair without recollecting facts or moving the cursor'

    def add_arguments(self, parser):
        parser.add_argument('--report-id', type=int, required=True)
        parser.add_argument('--expected-count', type=int, required=True)
        parser.add_argument('--expected-id-sha256', required=True)
        parser.add_argument('--expected-facts-sha256', required=True)
        parser.add_argument('--full-only', action='store_true', help='仅更新正常全量 HTML，保持职业报告原文件不变')

    def handle(self, *args, **options):
        report_id, expected_count = options['report_id'], options['expected_count']
        digest = options['expected_id_sha256']
        facts_digest = options['expected_facts_sha256']
        full_only = bool(options['full_only'])
        if (report_id <= 0 or expected_count <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or not re.fullmatch(r'[0-9a-f]{64}', facts_digest)):
            raise CommandError('Specify an existing report and independently verified source identity')
        try:
            row = WowHotfixReport.objects.get(pk=report_id)
        except WowHotfixReport.DoesNotExist as exc:
            raise CommandError('Hotfix report not found') from exc
        if (not row.collection_complete or row.branch != 'wow' or row.locale != 'enUS'
                or row.region_id <= 0 or row.from_push <= 0 or row.to_push <= row.from_push
                or row.entry_count != expected_count or (not full_only and not row.class_spell_count) or not row.build_str
                or not row.build_num or not row.summary_title or not row.wago_url):
            raise CommandError('Complete region-scoped Hotfix report required')
        if hashlib.sha256(row.source_facts_json.encode('utf-8')).hexdigest() != facts_digest:
            raise CommandError('Saved Hotfix fact payload SHA-256 mismatch')
        try:
            facts = json.loads(row.source_facts_json)
            sources = [item['source'] for item in facts]
            identities = {(int(source['push_id']), str(source['table_name']).lower(), int(source['record_id']))
                          for source in sources}
            valid = (len(facts) == len(sources) == len(identities) == expected_count
                     and len({int(source['id']) for source in sources}) == expected_count
                     and all(int(source['region_id']) == row.region_id and source['locale'] == row.locale
                             and row.from_push < int(source['push_id']) <= row.to_push for source in sources)
                     and source_ids_sha256(sources) == digest)
        except (TypeError, KeyError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise CommandError('Saved Hotfix sources do not match the verified report interval and fingerprint')

        root = Path(settings.BASE_DIR).resolve()
        public = (root / 'static' / 'portal' / 'reports').resolve()
        private = root / 'tmp' / 'wago-hotfix-staging'
        private.mkdir(parents=True, exist_ok=True)
        targets = []
        for relative in ((row.content_html_path,) if full_only else
                         (row.content_html_path, row.class_content_html_path)):
            if not relative or not relative.startswith('portal/reports/') or not relative.endswith('.html'):
                raise CommandError('Invalid existing Hotfix report file path')
            target = (root / 'static' / relative).resolve()
            if target.parent != public or not target.is_file():
                raise CommandError('Hotfix report file is missing or outside its publication directory')
            targets.append(target)
        if len(set(targets)) != len(targets):
            raise CommandError('Hotfix report projections must be separate files')
        if full_only and row.class_content_html_path == row.content_html_path:
            raise CommandError('Full report must not share its path with the class report')

        lock_path = root / 'tmp' / f'wago-hotfix-scan-r{row.region_id}.lock'
        staged = []
        backups = []
        with lock_path.open('a+') as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CommandError('Hotfix region is currently scanning; no report files were replaced') from exc
            try:
                monitor = WagoSkillDiffMonitor(None, None)
                monitor._hotfix_report_only = True
                cls = None
                if not full_only:
                    cls = monitor._generate_hotfix_class_report(
                        row.branch, row.build_str, row.from_push, row.to_push, region_id=row.region_id,
                        facts=facts, locale=row.locale, stage_for_publication=True,
                    )
                    staged.append(cls)
                # content_md is already stored and is not being changed here.
                # Re-render the same HTML generator directly, avoiding its
                # separate, unused Markdown DB2 enrichment round-trip.
                by_table = {}
                for source in sources:
                    by_table.setdefault(source['table_name'], []).append(source)
                table_stats = sorted(
                    ((name, len(items)) for name, items in by_table.items()),
                    key=lambda item: (-item[1], item[0].lower()),
                )
                max_enrich_setting = getattr(settings, 'WAGO_HOTFIX_REPORT_ENRICH_MAX', 50)
                max_enrich = 0 if full_only else (50 if max_enrich_setting is None else int(max_enrich_setting))
                full_path, full_relative = monitor._write_hotfix_full_html(
                    branch=row.branch, locale=row.locale, to_push=row.to_push,
                    summary_title=row.summary_title, wago_url=row.wago_url,
                    build_num=row.build_num, from_push=row.from_push,
                    table_stats=table_stats, by_table=by_table,
                    sample_per_table=int(getattr(settings, 'WAGO_HOTFIX_REPORT_SAMPLE_PER_TABLE', 20) or 20),
                    enrich_max=max_enrich, db2_build=row.build_str, region_id=row.region_id,
                    facts=facts, stage_for_publication=True,
                )
                full = {'content_html_path': full_relative, 'staging_path': full_path,
                        'entry_count': len(sources), 'table_count': len(table_stats)}
                staged.append(full)
                if (not full or full.get('entry_count') != row.entry_count
                        or full.get('table_count') != row.table_count
                        or full.get('content_html_path') != row.content_html_path
                        or (not full_only and (not cls
                            or cls.get('spell_count') != row.class_spell_count
                            or cls.get('class_count') != row.class_class_count
                            or cls.get('unresolved_count') != row.class_unresolved_count
                            or cls.get('content_html_path') != row.class_content_html_path))):
                    raise CommandError('Re-rendered Hotfix report identity or counts changed')
                paths = []
                for result in ((full,) if full_only else (full, cls)):
                    source = Path(result.get('staging_path') or '').resolve()
                    if source.parent != private.resolve() or not source.is_file():
                        raise CommandError('Re-rendered Hotfix report staging file is missing')
                    paths.append(source)
                full_text = paths[0].read_text(encoding='utf-8')
                class_text = paths[1].read_text(encoding='utf-8') if not full_only else ''
                if (full_text.count("<article class='record'") != expected_count
                        or (not full_only and class_text.count("<article class='spell'") != row.class_spell_count)):
                    raise CommandError('Re-rendered HTML omitted frozen source records or class spells')
                expected_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
                for target in targets:
                    fd, backup = tempfile.mkstemp(prefix='hotfix-backup-', suffix='.html', dir=private)
                    os.close(fd)
                    shutil.copy2(target, backup)
                    backups.append((target, Path(backup)))
                try:
                    for source, target in zip(paths, targets):
                        os.replace(source, target)
                    for target, digest_after in zip(targets, expected_hashes):
                        if hashlib.sha256(target.read_bytes()).hexdigest() != digest_after:
                            raise OSError('Published report file hash mismatch')
                except Exception as exc:
                    failures = []
                    for target, backup in backups:
                        try:
                            os.replace(backup, target)
                        except OSError as restore_exc:
                            failures.append(str(restore_exc))
                    if failures:
                        raise CommandError('Hotfix report rollback failed: ' + '; '.join(failures)) from exc
                    raise CommandError('Hotfix report publication failed; original files restored') from exc
            finally:
                for result in staged:
                    source = str((result or {}).get('staging_path') or '')
                    if source and Path(source).parent.resolve() == private.resolve():
                        Path(source).unlink(missing_ok=True)
                for _target, backup in backups:
                    backup.unlink(missing_ok=True)
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        self.stdout.write(self.style.SUCCESS(
            f'Hotfix report #{row.pk} re-rendered from {expected_count} frozen sources; '
            'report facts and cursor unchanged'
        ))
