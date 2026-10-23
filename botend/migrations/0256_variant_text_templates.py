"""增加变体共享文本容器；存量转换由有界维护任务执行。"""
from django.db import migrations, models


def protect_reverse(apps, schema_editor):
    variants = apps.get_model('botend', 'WowItemVariantSnapshot')
    if variants.objects.using(schema_editor.connection.alias).filter(effects_json__has_key='$variant_text').exists():
        raise RuntimeError('仍有变体引用共享文本，不能删除模板字段。请先恢复变体原文再回退迁移。')


class Migration(migrations.Migration):
    dependencies = [('botend', '0255_use_current_talent_metadata')]
    operations = [
        migrations.AddField(model_name='wowitemsnapshot', name='variant_text_templates',
                            field=models.JSONField(default=dict, blank=True, help_text='变体共享的无损文本模板')),
        migrations.AlterField(model_name='wowitemvariantsnapshot', name='effect_values',
                              field=models.JSONField(default=dict, blank=True, help_text='中央特效翻译引用与此变体参数')),
        migrations.RunPython(migrations.RunPython.noop, protect_reverse),
    ]
