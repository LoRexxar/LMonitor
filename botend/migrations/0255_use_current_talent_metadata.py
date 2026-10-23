"""向前解除0254身份保留：先检查冲突，再分批清理退役结构节点。"""

import django.db.models.deletion
from django.db import migrations, models


def discard_retired_nodes(apps, schema_editor):
    Node = apps.get_model('botend', 'WowTalentNodeMetadata')
    rows = Node.all_objects.using(schema_editor.connection.alias)
    retained = rows.exclude(localization_only=False, is_current=False)
    keys = ('talent_version_id', 'class_name', 'spec_name', 'tree_type', 'node_id', 'spell_id')
    # SQL唯一约束允许NULL；只拒绝恢复原约束时真正冲突的当前记录，不猜测保留哪一条。
    conflicts = (retained.filter(talent_version_id__isnull=False, node_id__isnull=False,
                                 spell_id__isnull=False).values(*keys)
                 .annotate(total=models.Count('pk')).filter(total__gt=1))
    if conflicts.exists():
        raise RuntimeError('当前天赋节点存在重复，0255未清理任何节点；先核对：' + str(list(conflicts[:10])))
    retired = rows.filter(localization_only=False, is_current=False)
    # 迁移可中断续跑；每批独立提交，不生成一个覆盖所有历史的大事务。
    while True:
        ids = list(retired.order_by('pk').values_list('pk', flat=True)[:500])
        if not ids:
            break
        retired.filter(pk__in=ids).delete()


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('botend', '0254_preserve_talent_node_identity'),
    ]

    operations = [
        migrations.RunPython(discard_retired_nodes, atomic=False),
        migrations.AlterUniqueTogether(
            name='wowtalentnodemetadata',
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name='wowtalentnodemetadata',
            name='is_current',
            field=models.BooleanField(db_index=True, default=True, help_text='是否属于当前分支节点集合；非当前节点可清理'),
        ),
        migrations.AlterField(
            model_name='wowtalentnodemetadata',
            name='talent_version',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='nodes', to='botend.wowtalentversion'),
        ),
        migrations.AlterUniqueTogether(
            name='wowtalentnodemetadata',
            unique_together={('talent_version', 'class_name', 'spec_name', 'tree_type', 'node_id', 'spell_id')},
        ),
    ]
