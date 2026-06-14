# -*- coding: utf-8 -*-

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db import close_old_connections
from django.db.utils import OperationalError

from botend.models import PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking
from botend.wow.talents.service import TalentBuildCodeService


class Command(BaseCommand):
    help = '迁移天赋字符串到 talent_build_code，并清理 talents_json 中的 build_code 脏节点'

    MODELS = (
        ('PlayerSpecTopPlayer', PlayerSpecTopPlayer),
        ('SpecDungeonRanking', SpecDungeonRanking),
        ('SpecRaidRanking', SpecRaidRanking),
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='仅统计，不写库')
        parser.add_argument('--model', default='', help='仅处理指定模型名')
        parser.add_argument('--batch-size', type=int, default=200, help='每批处理条数')

    def handle(self, *args, **options):
        dry_run = bool(options.get('dry_run'))
        model_filter = (options.get('model') or '').strip()
        batch_size = max(20, int(options.get('batch_size') or 200))
        for label, model in self.MODELS:
            if model_filter and label != model_filter:
                continue
            self._migrate_model(label, model, dry_run=dry_run, batch_size=batch_size)

    def _migrate_model(self, label, model, dry_run=False, batch_size=200):
        total = 0
        migrated = 0
        cleaned = 0
        missing = 0

        last_id = 0
        while True:
            close_old_connections()
            batch = self._fetch_batch(model, last_id, batch_size)
            if not batch:
                break
            for row in batch:
                total += 1
                build_code = TalentBuildCodeService.extract_build_code(
                    getattr(row, 'talent_build_code', ''),
                    getattr(row, 'talents_json', None),
                )
                cache_payload = self._strip_build_code_nodes(getattr(row, 'talents_json', None))

                changed_fields = []
                if getattr(row, 'talent_build_code', '') != build_code:
                    row.talent_build_code = build_code
                    changed_fields.append('talent_build_code')
                    if build_code:
                        migrated += 1
                if getattr(row, 'talents_json', None) != cache_payload:
                    row.talents_json = cache_payload
                    changed_fields.append('talents_json')
                    cleaned += 1

                if not build_code:
                    missing += 1

                if changed_fields and not dry_run:
                    self._save_row(row, changed_fields)
                last_id = row.id

        self.stdout.write(
            self.style.SUCCESS(
                f'{label}: total={total} migrated={migrated} cleaned={cleaned} missing={missing} dry_run={dry_run}'
            )
        )

    @staticmethod
    def _fetch_batch(model, last_id, batch_size):
        def _query():
            return list(model.objects.filter(id__gt=last_id).order_by('id')[:batch_size])

        try:
            return _query()
        except OperationalError:
            close_old_connections()
            return _query()

    @staticmethod
    def _save_row(row, changed_fields):
        def _save():
            with transaction.atomic():
                row.save(update_fields=changed_fields)

        try:
            _save()
        except OperationalError:
            close_old_connections()
            _save()

    @staticmethod
    def _strip_build_code_nodes(talents_json):
        if isinstance(talents_json, str):
            return []
        if not isinstance(talents_json, list):
            return talents_json if talents_json is not None else []
        cleaned = []
        for item in talents_json:
            if not isinstance(item, dict):
                continue
            build_code = TalentBuildCodeService.extract_build_code(talents_json=item)
            if build_code:
                continue
            cleaned.append(item)
        return cleaned
