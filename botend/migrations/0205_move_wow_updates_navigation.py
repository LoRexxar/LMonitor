from django.db import migrations


OLD_URL = '/#section-wow-skill-diff'
NEW_URL = '/portal/wow-updates/'


def move_navigation(apps, schema_editor):
    """保留已有导航配置，将首页锚点迁移为独立页面。"""
    items = apps.get_model('botend', 'PortalNavigationItem')
    items.objects.filter(url=OLD_URL).update(url=NEW_URL)


def restore_navigation(apps, schema_editor):
    items = apps.get_model('botend', 'PortalNavigationItem')
    items.objects.filter(url=NEW_URL).update(url=OLD_URL)


class Migration(migrations.Migration):
    dependencies = [('botend', '0204_move_mythicstats_navigation')]
    operations = [migrations.RunPython(move_navigation, restore_navigation)]
