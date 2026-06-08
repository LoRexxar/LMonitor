from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0073_wowwagomonitorstate_hotfix_region'),
    ]

    def _drop_hotfix_indexes_for_sqlite(apps, schema_editor):
        """
        SQLite 在 drop column 时会重建表/索引；如果索引引用了即将删除的字段，会直接报错。
        线上（MySQL）不受影响，这里仅对 SQLite 做兼容处理。
        """
        if getattr(schema_editor.connection, "vendor", "") != "sqlite":
            return
        try:
            schema_editor.execute("DROP INDEX IF EXISTS wow_wago_mo_hotfix_4f52f6_idx;")
        except Exception:
            # 兼容：某些 SQLite 版本/场景不支持 IF EXISTS 或索引名不同
            try:
                schema_editor.execute("DROP INDEX wow_wago_mo_hotfix_4f52f6_idx;")
            except Exception:
                pass

    operations = [
        migrations.RunPython(_drop_hotfix_indexes_for_sqlite, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='wowwagomonitorstate',
            name='hotfix_build',
        ),
        migrations.RemoveField(
            model_name='wowwagomonitorstate',
            name='hotfix_region',
        ),
    ]

