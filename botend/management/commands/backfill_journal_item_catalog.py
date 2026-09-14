from django.core.management.base import BaseCommand
from django.db import transaction

from botend.journal_models import JournalState
from botend.services.wow_item_catalog_import import upsert_journal_base_item_facts


class Command(BaseCommand):
    help = '从当前活动冒险手册 release 幂等补全中央物品基础事实（不访问外部数据）'

    def handle(self, *args, **options):
        state = (
            JournalState.objects.select_related('active_release')
            .filter(pk='wow-zhCN')
            .first()
        )
        release = state.active_release if state else None
        if not release:
            self.stdout.write('当前没有活动冒险手册 release，跳过中央物品回填')
            return

        manifest = release.manifest if isinstance(release.manifest, dict) else {}
        overlays = manifest.get('ptr_overlays') or {}
        retail_build = str(manifest.get('retail_build') or release.build or '')
        rows_by_build = {}
        instances = release.instances.prefetch_related('encounters').all()
        for instance in instances:
            overlay = overlays.get(str(instance.journal_id)) or {}
            build = str(overlay.get('source_build') or retail_build)
            row = dict(instance.payload or {})
            row['encounters'] = [dict(encounter.payload or {}) for encounter in instance.encounters.all()]
            rows_by_build.setdefault(build, []).append(row)

        item_ids = set()
        for rows in rows_by_build.values():
            for row in rows:
                for encounter in row.get('encounters') or []:
                    item_ids.update(int(loot['item_id']) for loot in encounter.get('loot') or [])

        with transaction.atomic():
            for build, rows in rows_by_build.items():
                upsert_journal_base_item_facts(rows, build)
        self.stdout.write(self.style.SUCCESS(
            f'中央物品目录回填完成：release={release.pk}，build={len(rows_by_build)}，物品={len(item_ids)}'
        ))
