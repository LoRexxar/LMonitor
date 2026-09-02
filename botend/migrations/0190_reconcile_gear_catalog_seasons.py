from django.db import migrations


SEASON_ALIASES = {
    'midnight-s2': 'mn-s2',
}


def reconcile_gear_catalog_seasons(apps, schema_editor):
    SeasonMeta = apps.get_model('botend', 'SeasonMeta')
    Variant = apps.get_model('botend', 'WowItemVariantSnapshot')

    for alias_key, canonical_key in SEASON_ALIASES.items():
        source = SeasonMeta.objects.filter(season_key=alias_key).first()
        if not source:
            continue
        target = SeasonMeta.objects.filter(season_key=canonical_key).first()
        if not target:
            source.season_key = canonical_key
            source.save(update_fields=('season_key', 'updated_at'))
            continue

        # 批次键在赛季内唯一。目标赛季没有同名批次时可无损迁移全部变体；
        # 已有同名批次则保留原记录，避免迁移过程删除任何装备数据。
        source_batches = list(
            Variant.objects.filter(season_id=source.pk)
            .values_list('batch_key', flat=True).distinct()
        )
        for batch_key in source_batches:
            if Variant.objects.filter(season_id=target.pk, batch_key=batch_key).exists():
                continue
            Variant.objects.filter(season_id=source.pk, batch_key=batch_key).update(
                season_id=target.pk,
            )

        source_catalog_exists = bool(
            source.gear_batch_key
            and Variant.objects.filter(
                season_id__in=(source.pk, target.pk),
                batch_key=source.gear_batch_key,
            ).exists()
        )
        target_catalog_exists = bool(
            target.gear_batch_key
            and Variant.objects.filter(
                season_id=target.pk,
                batch_key=target.gear_batch_key,
            ).exists()
        )
        source_is_newer = (
            not target.gear_synced_at
            or bool(source.gear_synced_at and source.gear_synced_at > target.gear_synced_at)
        )
        if source_catalog_exists and (not target_catalog_exists or source_is_newer):
            for field in (
                'game_build', 'gear_batch_key', 'gear_sync_status',
                'gear_synced_at', 'gear_sync_report',
            ):
                setattr(target, field, getattr(source, field))
        if not target.delve_sources and source.delve_sources:
            target.delve_sources = source.delve_sources
        target.is_active = bool(target.is_active or source.is_active)
        target.save(update_fields=(
            'game_build', 'gear_batch_key', 'gear_sync_status',
            'gear_synced_at', 'gear_sync_report', 'delve_sources',
            'is_active', 'updated_at',
        ))

        source.is_active = False
        source.gear_batch_key = ''
        source.gear_sync_status = 'merged_alias'
        source.save(update_fields=(
            'is_active', 'gear_batch_key', 'gear_sync_status', 'updated_at',
        ))

    active_rows = list(SeasonMeta.objects.filter(is_active=True))
    if len(active_rows) <= 1:
        return
    catalog_rows = [
        row for row in active_rows
        if row.gear_batch_key and Variant.objects.filter(
            season_id=row.pk, batch_key=row.gear_batch_key,
        ).exists()
    ]
    candidates = catalog_rows or active_rows
    winner = max(
        candidates,
        key=lambda row: (
            row.gear_synced_at.timestamp() if row.gear_synced_at else 0,
            row.pk,
        ),
    )
    SeasonMeta.objects.filter(is_active=True).exclude(pk=winner.pk).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0189_gear_builder_online_storage'),
    ]

    operations = [
        migrations.RunPython(reconcile_gear_catalog_seasons, migrations.RunPython.noop),
    ]
