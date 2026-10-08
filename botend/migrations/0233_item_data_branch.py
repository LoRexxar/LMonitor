"""显式记录 DBC 分支；普通构建更新复用同一行。"""

from django.db import migrations, models


def backfill_branch(apps, schema_editor):
    rows = apps.get_model('botend', 'WowItemVariantSnapshot').objects.using(schema_editor.connection.alias)
    ids = [row.pk for row in rows.only('id', 'metadata').iterator()
           if (row.metadata or {}).get('ptr_preview')]
    for start in range(0, len(ids), 1000):
        rows.filter(pk__in=ids[start:start + 1000]).update(data_branch='ptr')


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0232_wow_data_update'),
    ]

    operations = [
        migrations.AddField(
            model_name='wowitemvariantsnapshot',
            name='data_branch',
            field=models.CharField(choices=[('retail', '正式服'), ('ptr', '测试服'), ('beta', '内测服')], db_index=True, default='retail', max_length=16),
        ),
        migrations.RunPython(backfill_branch, migrations.RunPython.noop),
    ]
