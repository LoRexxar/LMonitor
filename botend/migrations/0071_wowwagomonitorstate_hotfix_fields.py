from django.db import migrations, models


def _migrate_hotfix_ext(apps, schema_editor):
    WowWagoMonitorState = apps.get_model('botend', 'WowWagoMonitorState')
    qs = WowWagoMonitorState.objects.all()
    for s in qs.iterator():
        ext_raw = (getattr(s, 'ext', '') or '').strip()
        if not ext_raw:
            continue
        try:
            import json
            ext = json.loads(ext_raw)
        except Exception:
            continue
        if not isinstance(ext, dict):
            continue
        push_id = ext.get('hotfix_push_id')
        if push_id is None:
            continue
        try:
            s.hotfix_push_id = int(str(push_id).strip() or '0')
        except Exception:
            s.hotfix_push_id = 0
        s.hotfix_build = str(ext.get('hotfix_build') or '').strip()
        s.hotfix_summary_title = str(ext.get('summary_title') or '').strip()[:255]
        try:
            s.hotfix_spell_count = int(ext.get('spell_count') or 0)
        except Exception:
            s.hotfix_spell_count = 0
        try:
            s.hotfix_class_count = int(ext.get('class_count') or 0)
        except Exception:
            s.hotfix_class_count = 0
        s.save(
            update_fields=[
                'hotfix_push_id',
                'hotfix_build',
                'hotfix_spell_count',
                'hotfix_class_count',
                'hotfix_summary_title',
            ]
        )


def _noop_reverse(apps, schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0070_remove_monitortask_proxy_extra_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_push_id',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_build',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_last_run_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_last_run_status',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_last_event_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_last_event_status',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_report_url',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_wago_url',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_spell_count',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_class_count',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_summary_title',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddIndex(
            model_name='wowwagomonitorstate',
            index=models.Index(fields=['hotfix_push_id'], name='wow_wago_mo_hotfix_b7502c_idx'),
        ),
        migrations.AddIndex(
            model_name='wowwagomonitorstate',
            index=models.Index(fields=['hotfix_last_run_at'], name='wow_wago_mo_hotfix_1118a0_idx'),
        ),
        migrations.AddIndex(
            model_name='wowwagomonitorstate',
            index=models.Index(fields=['hotfix_last_event_at'], name='wow_wago_mo_hotfix_4b8b02_idx'),
        ),
        migrations.RunPython(_migrate_hotfix_ext, _noop_reverse),
    ]

