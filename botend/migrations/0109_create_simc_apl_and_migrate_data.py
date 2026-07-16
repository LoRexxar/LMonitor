# Generated migration for SimcApl model and data migration from UserAplStorage + SimcContentTemplate APL types

import hashlib

from django.db import migrations, models


def normalize_active_apl_keys(apps, schema_editor):
    """Populate deterministic uniqueness keys and clean conflicts in legacy data."""
    SimcApl = apps.get_model('botend', 'SimcApl')
    used_keys = set()
    for apl in SimcApl.objects.order_by('id'):
        if not apl.is_active:
            continue
        owner = 'global' if apl.owner_user_id is None else str(apl.owner_user_id)
        spec = str(apl.spec or 'unknown').strip().lower()
        if apl.is_system:
            key = f'system:{owner}:{apl.source}:{spec}'
            if key in used_keys:
                SimcApl.objects.filter(id=apl.id).update(is_active=False, active_unique_key=None)
                continue
        else:
            normalized_name = ' '.join(str(apl.name or '').strip().lower().split())
            name_hash = hashlib.sha256(normalized_name.encode('utf-8')).hexdigest()
            key = f'user:{owner}:{spec}:{name_hash}'
            if key in used_keys:
                suffix = 2
                base_name = str(apl.name or 'Migrated APL').strip()
                while True:
                    migrated_name = f'{base_name} (迁移 {suffix})'
                    normalized_name = ' '.join(migrated_name.lower().split())
                    name_hash = hashlib.sha256(normalized_name.encode('utf-8')).hexdigest()
                    key = f'user:{owner}:{spec}:{name_hash}'
                    if key not in used_keys:
                        SimcApl.objects.filter(id=apl.id).update(name=migrated_name)
                        break
                    suffix += 1
        SimcApl.objects.filter(id=apl.id).update(active_unique_key=key)
        used_keys.add(key)


def migrate_apl_data_forward(apps, schema_editor):
    """
    Migrate data from UserAplStorage and SimcContentTemplate (default_apl/custom_apl) to SimcApl.
    Handle ID/name conflicts, preserve owner/content/spec/class/source/active/selectable/timestamps.
    Use 'unknown' as default spec for old personal data without spec.

    IMPORTANT: UserAplStorage in 0108 has no spec field, all old data gets spec='unknown'.
    """
    SimcApl = apps.get_model('botend', 'SimcApl')
    UserAplStorage = apps.get_model('botend', 'UserAplStorage')
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')

    # Migrate UserAplStorage (personal APL) to SimcApl
    # Historical UserAplStorage in 0108 had no spec field, so all get 'unknown'
    for old_apl in UserAplStorage.objects.all():
        SimcApl.objects.create(
            name=old_apl.title,
            spec='unknown',
            class_name='',
            content=old_apl.apl_code,
            source='user',
            is_system=False,
            owner_user_id=old_apl.user_id,
            is_active=old_apl.is_active,
            is_selectable=True,
            sync_version='',
        )

    # Migrate SimcContentTemplate TYPE_DEFAULT_APL to SimcApl (system default APL)
    default_apls = SimcContentTemplate.objects.filter(template_type='default_apl')
    for template in default_apls:
        SimcApl.objects.create(
            name=template.name or f'Default APL {template.spec}',
            spec=template.spec or 'unknown',
            class_name=template.class_name or '',
            content=template.content,
            source=template.source,
            is_system=template.owner_user_id is None,
            owner_user_id=None if template.owner_user_id is None else template.owner_user_id,
            is_active=template.is_active,
            is_selectable=template.is_selectable,
            sync_version=template.sync_version or '',
            created_at=template.created_at,
            updated_at=template.updated_at,
        )

    # Migrate SimcContentTemplate TYPE_CUSTOM_APL to SimcApl (personal APL from old system)
    custom_apls = SimcContentTemplate.objects.filter(template_type='custom_apl')
    for template in custom_apls:
        # A legacy custom row without an owner has no safe personal owner.
        # Preserve it as a global read-only resource instead of creating an
        # orphan that no user can query.
        has_owner = template.owner_user_id is not None
        SimcApl.objects.create(
            name=template.name or f'Custom APL {template.spec}',
            spec=template.spec or 'unknown',
            class_name=template.class_name or '',
            content=template.content,
            source='user' if has_owner else 'simc_upstream',
            is_system=not has_owner,
            owner_user_id=template.owner_user_id if has_owner else None,
            is_active=template.is_active,
            is_selectable=template.is_selectable,
            sync_version=template.sync_version or '',
            created_at=template.created_at,
            updated_at=template.updated_at,
        )


def migrate_apl_data_backward(apps, schema_editor):
    """
    Reverse migration: move SimcApl back to UserAplStorage and SimcContentTemplate.
    NOTE: UserAplStorage in 0108 has no spec field, omit it entirely.
    """
    SimcApl = apps.get_model('botend', 'SimcApl')
    UserAplStorage = apps.get_model('botend', 'UserAplStorage')
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')

    for apl in SimcApl.objects.all():
        if apl.is_system:
            # System APL -> SimcContentTemplate default_apl
            SimcContentTemplate.objects.create(
                name=apl.name,
                template_type='default_apl',
                source=apl.source,
                spec=apl.spec,
                class_name=apl.class_name,
                content=apl.content,
                is_active=apl.is_active,
                is_selectable=apl.is_selectable,
                sync_version=apl.sync_version,
                owner_user_id=apl.owner_user_id,
            )
        else:
            # Personal APL -> UserAplStorage (no spec field in 0108)
            UserAplStorage.objects.create(
                user_id=apl.owner_user_id or 0,
                title=apl.name,
                apl_code=apl.content,
                is_active=apl.is_active,
            )


def delete_migrated_apl_rows_forward(apps, schema_editor):
    """Delete old APL rows from UserAplStorage and SimcContentTemplate after successful migration."""
    UserAplStorage = apps.get_model('botend', 'UserAplStorage')
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')

    # Delete all UserAplStorage
    UserAplStorage.objects.all().delete()

    # Delete default_apl and custom_apl from SimcContentTemplate
    SimcContentTemplate.objects.filter(template_type__in=['default_apl', 'custom_apl']).delete()


def restore_migrated_apl_rows_backward(apps, schema_editor):
    """Restore is handled by migrate_apl_data_backward, no-op here."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0108_simc_batch_request_manifest_and_status'),
    ]

    operations = [
        # Step 1: Create SimcApl table
        migrations.CreateModel(
            name='SimcApl',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(help_text='APL名称', max_length=200)),
                ('spec', models.CharField(help_text='适用专精标识，如 warrior_fury', max_length=100)),
                ('class_name', models.CharField(blank=True, default='', help_text='职业英文名，如 warrior', max_length=50)),
                ('content', models.TextField(help_text='APL代码内容')),
                ('source', models.CharField(choices=[('simc_upstream', 'SimC源码同步'), ('user', '用户维护')], default='user', help_text='内容来源', max_length=32)),
                ('is_system', models.BooleanField(default=False, help_text='是否为系统默认APL（只读）')),
                ('owner_user_id', models.BigIntegerField(blank=True, help_text='所属用户ID，NULL表示全局默认APL', null=True)),
                ('is_active', models.BooleanField(default=True, help_text='是否启用')),
                ('is_selectable', models.BooleanField(default=True, help_text='任务发起时是否可选择')),
                ('sync_version', models.CharField(blank=True, default='', help_text='同步来源版本/提交', max_length=128)),
                ('active_unique_key', models.CharField(blank=True, help_text='活跃 APL 唯一键；停用时为 NULL', max_length=255, null=True, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'SimC APL',
                'verbose_name_plural': 'SimC APL',
                'db_table': 'simc_apl',
            },
        ),
        migrations.AddIndex(
            model_name='simcapl',
            index=models.Index(fields=['spec', 'is_active'], name='simc_apl_sp_ac_idx'),
        ),
        migrations.AddIndex(
            model_name='simcapl',
            index=models.Index(fields=['owner_user_id', '-created_at'], name='simc_apl_ow_cr_idx'),
        ),
        migrations.AddIndex(
            model_name='simcapl',
            index=models.Index(fields=['source', 'is_system'], name='simc_apl_so_sy_idx'),
        ),

        # Step 2: Migrate data from UserAplStorage and SimcContentTemplate to SimcApl
        migrations.RunPython(migrate_apl_data_forward, migrate_apl_data_backward),

        # Step 2b: normalize legacy duplicates before the old stores are removed.
        migrations.RunPython(normalize_active_apl_keys, migrations.RunPython.noop),

        # Step 3: Delete old APL rows from SimcContentTemplate (default_apl/custom_apl)
        migrations.RunPython(delete_migrated_apl_rows_forward, restore_migrated_apl_rows_backward),

        # Step 4: Delete UserAplStorage model (state + database)
        migrations.DeleteModel(name='UserAplStorage'),
        migrations.AlterModelOptions(
            name='simccontenttemplate',
            options={'verbose_name': 'SimC模板', 'verbose_name_plural': 'SimC模板'},
        ),
        migrations.AlterField(
            model_name='simccontenttemplate',
            name='template_type',
            field=models.CharField(
                choices=[
                    ('base_template', '基础模板'),
                    ('default_player', '默认玩家装备模板'),
                    ('custom_player', '用户自定义装备'),
                ],
                default='base_template',
                help_text='内容类型',
                max_length=32,
            ),
        ),
    ]
