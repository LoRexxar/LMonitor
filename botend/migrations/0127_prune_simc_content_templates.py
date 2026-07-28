from django.db import migrations, models


def prune_content_templates(apps, schema_editor):
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')
    templates = SimcContentTemplate.objects.all()

    # APL has its own resource model; custom player templates are obsolete.
    templates.exclude(template_type__in=('base_template', 'default_player')).delete()

    base_templates = SimcContentTemplate.objects.filter(template_type='base_template')
    keeper = (
        base_templates.filter(
            owner_user_id__isnull=True,
            spec='default',
            is_active=True,
        ).order_by('-is_selectable', '-id').first()
        or base_templates.filter(owner_user_id__isnull=True, is_active=True).order_by('-id').first()
        or base_templates.filter(owner_user_id__isnull=True).order_by('-id').first()
    )
    if not keeper:
        # Do not invent executable template content in a data migration.
        return

    base_templates.exclude(pk=keeper.pk).delete()
    changed_fields = []
    for field, value in (
        ('owner_user_id', None),
        ('spec', 'default'),
        ('class_name', ''),
        ('is_active', True),
        ('is_selectable', True),
        ('active_unique_key', 'base_template:global:default'),
    ):
        if getattr(keeper, field) != value:
            setattr(keeper, field, value)
            changed_fields.append(field)
    if changed_fields:
        keeper.save(update_fields=changed_fields)


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0126_delete_inactive_simc_profiles'),
    ]

    operations = [
        migrations.AlterField(
            model_name='simccontenttemplate',
            name='template_type',
            field=models.CharField(
                choices=[
                    ('base_template', '基础模板'),
                    ('default_player', '默认玩家装备模板'),
                ],
                default='base_template',
                help_text='内容类型',
                max_length=32,
            ),
        ),
        migrations.RunPython(prune_content_templates, migrations.RunPython.noop),
    ]
