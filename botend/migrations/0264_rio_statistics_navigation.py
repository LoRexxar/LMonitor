"""为统计观察页增加可由既有管理界面维护的入口。"""
from django.db import migrations


def add_navigation(apps, schema_editor):
    groups = apps.get_model('botend', 'PortalNavigationGroup')
    items = apps.get_model('botend', 'PortalNavigationItem')
    if items.objects.filter(url='/portal/rio-statistics/').exists():
        return
    group, _ = groups.objects.get_or_create(key='data', defaults={
        'name': '数据中心', 'description': '职业、赛季与副本统计', 'icon_key': 'chart', 'sort_order': 20})
    items.objects.create(group=group, name='魔兽世界数据观察', url='/portal/rio-statistics/',
                         desc='参与角色与分数趋势、空间热力图、团本公会进度', icon_key='chart', sort_order=35)


class Migration(migrations.Migration):
    dependencies = [('botend', '0263_spec_dungeon_fight_started_at')]
    operations = [migrations.RunPython(add_navigation, migrations.RunPython.noop)]
