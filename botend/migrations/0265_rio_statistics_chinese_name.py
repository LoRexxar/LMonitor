"""仅更新本功能默认入口名称，不覆盖管理员自定义入口。"""
from django.db import migrations


def rename_navigation(apps, schema_editor):
    apps.get_model('botend', 'PortalNavigationItem').objects.filter(
        url='/portal/rio-statistics/', name__in=['赛季数据观察', '艾泽拉斯数据观察'],
    ).update(name='魔兽世界数据观察')


class Migration(migrations.Migration):
    dependencies = [('botend', '0264_rio_statistics_navigation')]
    operations = [migrations.RunPython(rename_navigation, migrations.RunPython.noop)]
