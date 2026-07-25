from django.db import migrations, models
import django.db.models.deletion
from django.db.models.functions import Concat


PRODUCTION_IDENTIFIER = 'production'


def bind_production_backend(apps, schema_editor):
    Backend = apps.get_model('botend', 'SimcBackendBinary')
    Task = apps.get_model('botend', 'SimcTask')
    backend = Backend.objects.order_by('id').first()
    if backend is None:
        backend = Backend.objects.create(
            identifier=PRODUCTION_IDENTIFIER,
            name='正式服',
            platform='linux64',
            simc_path='',
            is_active=True,
        )
    else:
        backend.identifier = PRODUCTION_IDENTIFIER
        backend.name = '正式服'
        backend.is_active = True
        backend.save(update_fields=['identifier', 'name', 'is_active'])

    Backend.objects.exclude(pk=backend.pk).filter(identifier='').update(
        identifier=Concat(models.Value('backend-'), models.F('id')),
        name=Concat(models.Value('SimC 后端 #'), models.F('id')),
    )
    Task.objects.update(backend_id=backend.pk)


class Migration(migrations.Migration):
    dependencies = [('botend', '0124_complete_simc_secondary_stat_rules')]

    operations = [
        migrations.AddField(
            model_name='simcbackendbinary',
            name='identifier',
            field=models.SlugField(
                default='', help_text='稳定标识，如 production/ptr', max_length=64,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='name',
            field=models.CharField(
                default='', help_text='展示名称，如 正式服/PTR', max_length=100,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='is_active',
            field=models.BooleanField(default=True, help_text='是否允许新任务选择'),
        ),
        migrations.AddField(
            model_name='simctask',
            name='backend',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='tasks',
                to='botend.simcbackendbinary',
            ),
        ),
        migrations.RunPython(bind_production_backend, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='simcbackendbinary',
            name='identifier',
            field=models.SlugField(
                help_text='稳定标识，如 production/ptr', max_length=64, unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='simctask',
            name='backend',
            field=models.ForeignKey(
                help_text='本任务显式指定的 SimC 执行后端',
                on_delete=django.db.models.deletion.PROTECT,
                related_name='tasks',
                to='botend.simcbackendbinary',
            ),
        ),
    ]
