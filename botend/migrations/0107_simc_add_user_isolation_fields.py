# Generated manually for user isolation fields
import hashlib
from django.db import migrations, models


def compute_active_unique_key_legacy(template_type, owner, spec, name=''):
    """
    Historical compute logic matching the model at migration time.
    Must be self-contained for historical migrations.
    """
    if template_type == 'custom_apl':
        normalized_name = (name or '').lower().strip()
        name_hash = hashlib.sha256(normalized_name.encode('utf-8')).hexdigest()[:16]
        return f'{template_type}:{owner}:{spec}:{name_hash}'
    elif template_type in ('base_template', 'default_apl', 'default_player'):
        if owner == 'global':
            return f'{template_type}:global:{spec}'
        else:
            return f'{template_type}:{owner}:{spec}'
    elif template_type == 'custom_player':
        return f'{template_type}:{owner}:{spec}'
    return None


def backfill_existing_templates(apps, schema_editor):
    """
    Backfill active_unique_key for existing active templates.
    Existing templates are treated as global (owner_user_id=NULL).
    For active duplicates within same type+spec, keep max(id) active, deactivate others.
    """
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')

    # Group active templates by their actual uniqueness scope.  Named custom
    # APLs may coexist for the same specialization, so their normalized name
    # must be part of the group key.
    from collections import defaultdict
    groups = defaultdict(list)

    for tpl in SimcContentTemplate.objects.filter(is_active=True):
        spec = tpl.spec or 'default'
        if tpl.template_type == 'custom_apl':
            key = (tpl.template_type, spec, (tpl.name or '').lower().strip())
        else:
            key = (tpl.template_type, spec)
        groups[key].append(tpl)

    # Process each group
    for group_key, templates in groups.items():
        template_type, spec = group_key[:2]
        if len(templates) == 1:
            # Single template: compute and set key
            tpl = templates[0]
            tpl.active_unique_key = compute_active_unique_key_legacy(
                template_type, 'global', spec, tpl.name
            )
            tpl.save(update_fields=['active_unique_key'])
        else:
            # Multiple active templates: keep max(id), deactivate others
            templates_sorted = sorted(templates, key=lambda t: t.id)
            kept = templates_sorted[-1]
            deactivated = templates_sorted[:-1]

            # Deactivate duplicates
            for tpl in deactivated:
                tpl.is_active = False
                tpl.active_unique_key = None
                tpl.save(update_fields=['is_active', 'active_unique_key'])

            # Set key for kept template
            kept.active_unique_key = compute_active_unique_key_legacy(
                template_type, 'global', spec, kept.name
            )
            kept.save(update_fields=['active_unique_key'])


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0106_simc_phase1_frozen_manifest'),
    ]

    operations = [
        migrations.AddField(
            model_name='simccontenttemplate',
            name='owner_user_id',
            field=models.BigIntegerField(blank=True, help_text='所属用户ID，NULL表示全局模板', null=True),
        ),
        migrations.AddField(
            model_name='simccontenttemplate',
            name='active_unique_key',
            field=models.CharField(blank=True, help_text='活跃时唯一键，非活跃时为NULL', max_length=200, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='simccontenttemplate',
            name='template_type',
            field=models.CharField(
                choices=[
                    ('base_template', '基础模板'),
                    ('default_apl', '默认APL'),
                    ('custom_apl', '个人APL'),
                    ('default_player', '默认玩家装备模板'),
                    ('custom_player', '用户自定义装备'),
                ],
                default='base_template',
                help_text='内容类型',
                max_length=32
            ),
        ),
        migrations.RunPython(backfill_existing_templates, migrations.RunPython.noop),
    ]
