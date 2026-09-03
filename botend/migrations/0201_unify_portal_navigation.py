from django.db import migrations


def remove_obsolete_today_group(apps, schema_editor):
    PortalNavigationGroup = apps.get_model('botend', 'PortalNavigationGroup')
    PortalNavigationItem = apps.get_model('botend', 'PortalNavigationItem')
    PortalNavigationGroup.objects.filter(key='today').delete()
    PortalNavigationItem.objects.filter(url='/#section-tools').delete()


def restore_obsolete_today_group(apps, schema_editor):
    PortalNavigationGroup = apps.get_model('botend', 'PortalNavigationGroup')
    PortalNavigationItem = apps.get_model('botend', 'PortalNavigationItem')
    group, _ = PortalNavigationGroup.objects.update_or_create(
        key='today',
        defaults={
            'name': '今日速览',
            'description': '当天内容、活动与时间节点',
            'icon_key': 'calendar',
            'sort_order': 10,
            'is_active': True,
        },
    )
    defaults = (
        ('今日内容', '/#section-news', '今日魔兽、蓝帖与最新资讯', 10),
        ('活动提醒', '/#section-events', '游戏活动、世界事件与时间节点', 20),
    )
    for name, url, desc, sort_order in defaults:
        PortalNavigationItem.objects.update_or_create(
            group=group,
            url=url,
            defaults={
                'name': name,
                'desc': desc,
                'icon_key': 'calendar',
                'badge': '',
                'badge_tone': 'default',
                'sort_order': sort_order,
                'show_in_header': False,
                'show_in_home_guide': False,
                'is_active': True,
            },
        )
    tools_group = PortalNavigationGroup.objects.filter(key='tools').first()
    if tools_group:
        PortalNavigationItem.objects.update_or_create(
            group=tools_group,
            url='/#section-tools',
            defaults={
                'name': '快捷链接',
                'desc': '跳转到首页常用工具与站点目录',
                'icon_key': 'tools',
                'badge': '',
                'badge_tone': 'default',
                'sort_order': 75,
                'show_in_header': True,
                'show_in_home_guide': False,
                'is_active': True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0200_align_portal_navigation_categories'),
    ]

    operations = [
        migrations.RunPython(remove_obsolete_today_group, restore_obsolete_today_group),
        migrations.RemoveIndex(
            model_name='portalnavigationgroup',
            name='wow_portal__is_acti_e6ba79_idx',
        ),
        migrations.RemoveField(
            model_name='portalnavigationgroup',
            name='is_active',
        ),
        migrations.RemoveIndex(
            model_name='portalnavigationitem',
            name='wow_portal__show_in_819f54_idx',
        ),
        migrations.RemoveIndex(
            model_name='portalnavigationitem',
            name='wow_portal__show_in_32ea94_idx',
        ),
        migrations.RemoveField(
            model_name='portalnavigationitem',
            name='show_in_header',
        ),
        migrations.RemoveField(
            model_name='portalnavigationitem',
            name='show_in_home_guide',
        ),
    ]
