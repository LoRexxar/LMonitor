from django.db import migrations, models


def deduplicate_default_players(apps, schema_editor):
    template = apps.get_model('botend', 'SimcContentTemplate')
    groups = template.objects.filter(template_type='default_player').values_list('source', 'spec').distinct()
    for source, spec in groups.iterator():
        rows = template.objects.filter(
            template_type='default_player', source=source, spec=spec,
        ).order_by('-updated_at', '-id')
        keep = rows.first()
        if keep:
            rows.exclude(id=keep.id).delete()


class Migration(migrations.Migration):
    dependencies = [('botend', '0103_enable_standard_simc_raid_buffs')]

    operations = [
        migrations.AlterField(
            model_name='simccontenttemplate',
            name='template_type',
            field=models.CharField(
                choices=[
                    ('base_template', '基础模板'),
                    ('default_apl', '默认APL'),
                    ('custom_apl', '个人APL'),
                    ('default_player', '默认玩家装备模板'),
                ],
                default='base_template', help_text='内容类型', max_length=32,
            ),
        ),
        migrations.RunPython(deduplicate_default_players, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='simccontenttemplate',
            constraint=models.UniqueConstraint(
                condition=models.Q(template_type='default_player'),
                fields=('template_type', 'source', 'spec'),
                name='uniq_simc_default_player_source_spec',
            ),
        ),
    ]
