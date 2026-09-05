from django.db import migrations


URL = '/portal/mplus/dps-rankings/'


def add_mplus_dps_navigation(apps, schema_editor):
    Group = apps.get_model('botend', 'PortalNavigationGroup')
    Item = apps.get_model('botend', 'PortalNavigationItem')
    group = Group.objects.filter(key='data').first()
    if group is None:
        group = Group.objects.create(
            key='data',
            name='职业与数据',
            description='专精页面与模拟结果',
            icon_key='chart',
            sort_order=20,
        )
    item = Item.objects.filter(group=group, url=URL).order_by('id').first()
    values = {
        'name': '大秘境 DPS 榜单',
        'desc': '8 副本加权与单副本职业伤害排名',
        'icon_key': 'chart',
        'badge': 'NEW',
        'badge_tone': 'new',
        'sort_order': 65,
        'is_active': True,
    }
    if item is None:
        Item.objects.create(group=group, url=URL, **values)
    else:
        for key, value in values.items():
            setattr(item, key, value)
        item.save(update_fields=list(values))


def remove_mplus_dps_navigation(apps, schema_editor):
    Item = apps.get_model('botend', 'PortalNavigationItem')
    Item.objects.filter(url=URL).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0202_monitortasklease'),
    ]

    operations = [
        migrations.RunPython(add_mplus_dps_navigation, remove_mplus_dps_navigation),
    ]
