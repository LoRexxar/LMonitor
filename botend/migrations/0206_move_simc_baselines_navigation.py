from django.db import migrations


OLD_URL = '/#section-simc-baselines'
NEW_URL = '/portal/simc-benchmarks/'
OLD_DESC = '查看首页公开的职业模拟任务'
NEW_DESC = '查看公开的职业模拟任务与结果'


def move_navigation(apps, schema_editor):
    """将首页列表入口迁往已有模拟数据页面，保留自定义导航配置。"""
    items = apps.get_model('botend', 'PortalNavigationItem')
    rows = items.objects.filter(url=OLD_URL)
    rows.filter(desc=OLD_DESC).update(desc=NEW_DESC)
    rows.update(url=NEW_URL)


class Migration(migrations.Migration):
    dependencies = [('botend', '0205_move_wow_updates_navigation')]
    # 独立页面早已存在，回退时保留有效链接，避免误改原本就指向它的入口。
    operations = [migrations.RunPython(move_navigation, migrations.RunPython.noop)]
