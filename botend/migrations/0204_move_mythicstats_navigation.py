from django.db import migrations


OLD_URL = '/#section-mythicstats'
NEW_URL = '/portal/mplus/dps-rankings/?source=mythicstats'


def move_navigation(apps, schema_editor):
    """让既有首页入口直接打开迁移后的来源页签。"""
    items = apps.get_model('botend', 'PortalNavigationItem')
    items.objects.filter(url=OLD_URL).update(url=NEW_URL)


def restore_navigation(apps, schema_editor):
    items = apps.get_model('botend', 'PortalNavigationItem')
    items.objects.filter(url=NEW_URL).update(url=OLD_URL)


class Migration(migrations.Migration):
    dependencies = [('botend', '0203_mplus_dps_rankings_navigation')]
    operations = [migrations.RunPython(move_navigation, restore_navigation)]
