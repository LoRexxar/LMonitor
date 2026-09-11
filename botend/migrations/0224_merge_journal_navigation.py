"""将冒险手册归入大秘境与团本分类，统一首页和顶栏入口。"""
from django.db import migrations


def merge_navigation(apps, schema_editor):
    alias = schema_editor.connection.alias
    Group = apps.get_model('botend', 'PortalNavigationGroup')
    Item = apps.get_model('botend', 'PortalNavigationItem')
    group, _ = Group.objects.using(alias).get_or_create(
        key='mythic', defaults={'name': '大秘境&团本', 'icon_key': 'chart', 'sort_order': 30},
    )
    Group.objects.using(alias).filter(pk=group.pk).update(
        name='大秘境&团本', description='副本手册、首领掉落、分数线与排行榜',
    )
    Item.objects.using(alias).filter(url='/portal/adventure-journal/').update(group=group)
    old_groups = Group.objects.using(alias).filter(key='adventure-journal')
    # 迁移分类内全部入口，避免删除分类时连带删除后台维护的条目。
    Item.objects.using(alias).filter(group_id__in=old_groups.values('pk')).update(group=group)
    old_groups.delete()


class Migration(migrations.Migration):
    dependencies = [('botend', '0223_adventure_journal_navigation')]
    operations = [migrations.RunPython(merge_navigation, migrations.RunPython.noop)]
