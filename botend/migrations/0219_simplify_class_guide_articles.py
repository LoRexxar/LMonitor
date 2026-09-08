"""将当前展示正文迁入文章，移除修订及审核机制。升级前应备份历史数据。"""

import hashlib
import json

from django.db import migrations, models
from django.utils import timezone


def content_hash(title, markdown):
    return hashlib.sha256(json.dumps([title, markdown], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ensure_idle(apps, schema_editor):
    Lease = apps.get_model('botend', 'MonitorTaskLease')
    if Lease.objects.using(schema_editor.connection.alias).filter(task__name='MaxrollClassGuideMonitor', expires_at__gt=timezone.now()).exists():
        raise RuntimeError('请先停止攻略同步，待任务结束或执行锁过期后重试迁移。')


def copy_current_content(apps, schema_editor):
    Guide = apps.get_model('botend', 'ClassGuide')
    Revision = apps.get_model('botend', 'ClassGuideRevision')
    alias = schema_editor.connection.alias
    for guide in Guide.objects.using(alias).iterator():
        records = Revision.objects.using(alias).filter(guide_id=guide.pk).order_by('-number')
        current = records.first()
        if current is None:
            continue
        if current.audit.get('manual_conflict'):
            current = records.filter(origin='manual').first() or current
        checks = {key: current.audit[key] for key in ('source_refs', 'untranslated', 'source_block_counts', 'unsupported') if key in current.audit}
        Guide.objects.using(alias).filter(pk=guide.pk).update(
            title=current.title, content_markdown=current.content_markdown,
            source_markdown=current.source_markdown, source_payload=current.source_payload,
            source_hash=current.source_hash, source_modified=current.source_modified, check_data=checks,
            imported_content_hash='' if current.origin == 'manual' else content_hash(current.title, current.content_markdown))


def restore_current_content(apps, schema_editor):
    # 仅恢复当前正文；历史修订需要从升级前的数据库备份恢复。
    from botend.services.class_guide_markdown import compile_markdown
    Guide = apps.get_model('botend', 'ClassGuide')
    Revision = apps.get_model('botend', 'ClassGuideRevision')
    alias = schema_editor.connection.alias
    for guide in Guide.objects.using(alias).iterator():
        Revision.objects.using(alias).create(guide_id=guide.pk, number=1,
            origin='translation' if guide.imported_content_hash == content_hash(guide.title, guide.content_markdown) else 'manual',
            title=guide.title, content_markdown=guide.content_markdown, source_markdown=guide.source_markdown,
            blocks=compile_markdown(guide.content_markdown), source_blocks=compile_markdown(guide.source_markdown),
            source_payload=guide.source_payload, source_hash=guide.source_hash,
            source_modified=guide.source_modified, audit=guide.check_data)
        Guide.objects.using(alias).filter(pk=guide.pk).update(revision_number=1)


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0218_consolidate_class_guide_monitor'),
    ]

    operations = [
        migrations.RunPython(ensure_idle, migrations.RunPython.noop),
        migrations.AddField(
            model_name='classguide',
            name='check_data',
            field=models.JSONField(blank=True, default=dict, verbose_name='内容检查数据'),
        ),
        migrations.AddField(
            model_name='classguide',
            name='content_markdown',
            field=models.TextField(blank=True, verbose_name='Markdown 正文'),
        ),
        migrations.AddField(
            model_name='classguide',
            name='imported_content_hash',
            field=models.CharField(blank=True, max_length=64, verbose_name='最近导入正文指纹'),
        ),
        migrations.AddField(
            model_name='classguide',
            name='source_hash',
            field=models.CharField(blank=True, db_index=True, max_length=64, verbose_name='来源指纹'),
        ),
        migrations.AddField(
            model_name='classguide',
            name='source_markdown',
            field=models.TextField(blank=True, verbose_name='原文 Markdown'),
        ),
        migrations.AddField(
            model_name='classguide',
            name='source_modified',
            field=models.CharField(blank=True, max_length=80, verbose_name='原文更新时间'),
        ),
        migrations.AddField(
            model_name='classguide',
            name='source_payload',
            field=models.JSONField(blank=True, default=dict, verbose_name='来源快照'),
        ),
        migrations.RunPython(copy_current_content, restore_current_content),
        migrations.RemoveField(
            model_name='classguide',
            name='published_revision',
        ),
        migrations.RemoveField(
            model_name='classguide',
            name='revision_number',
        ),
        migrations.DeleteModel(
            name='ClassGuideRevision',
        ),
    ]
