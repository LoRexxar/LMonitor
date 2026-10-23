"""允许同节点的不同天赋身份共存，并阻止版本级联删除历史节点。"""

import django.db.models.deletion
import django.db.models.manager
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0253_character_bindings'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='wowtalentnodemetadata',
            options={'base_manager_name': 'all_objects', 'verbose_name': 'WoW天赋与名称元数据', 'verbose_name_plural': 'WoW天赋与名称元数据'},
        ),
        migrations.AlterModelManagers(
            name='wowtalentnodemetadata',
            managers=[
                ('objects', django.db.models.manager.Manager()),
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
        migrations.AlterUniqueTogether(
            name='wowtalentnodemetadata',
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name='wowtalentnodemetadata',
            name='talent_version',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='nodes', to='botend.wowtalentversion'),
        ),
        migrations.AlterUniqueTogether(
            name='wowtalentnodemetadata',
            unique_together={('talent_version', 'class_name', 'spec_name', 'tree_type', 'node_id', 'spell_id', 'talent_id', 'localization_only')},
        ),
        # 新旧身份共存后旧唯一约束已不成立；禁止自动退回会破坏引用的旧生命周期。
        migrations.RunPython(migrations.RunPython.noop),
    ]
