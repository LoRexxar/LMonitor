import hashlib

from django.db import migrations


MYTHIC_GUIDE_LINKS = (
    {
        'name': '大秘境分数线',
        'url': '/#section-mplus-cutoffs',
        'desc': '查看各地区 0.1% / 1% 赛季分数门槛',
        'icon_key': 'chart',
        'topbar_order': 10,
    },
    {
        'name': 'Top Runs 排行',
        'url': '/#section-rank',
        'desc': '查看当前赛季最高层队伍与副本记录',
        'icon_key': 'chart',
        'topbar_order': 20,
    },
    {
        'name': '职业巅峰榜',
        'url': '/#section-peak-spec',
        'desc': '查看各专精顶尖玩家与巅峰分数',
        'icon_key': 'chart',
        'topbar_order': 30,
    },
    {
        'name': 'DPS 排行榜',
        'url': '/#section-mythicstats',
        'desc': '查看副本专精平均与峰值 DPS',
        'icon_key': 'chart',
        'topbar_order': 40,
    },
)


def configure_home_guide(apps, schema_editor):
    PortalToolLink = apps.get_model('botend', 'PortalToolLink')
    PortalToolLink.objects.filter(
        url__in=('/#section-news', '/#section-events'),
    ).update(show_in_guide=False)
    PortalToolLink.objects.filter(url='/#section-wow-skill-diff').update(
        category='community',
        topbar_order=25,
        sort_order=25,
    )

    for item in MYTHIC_GUIDE_LINKS:
        url_hash = hashlib.sha256(item['url'].encode('utf-8')).hexdigest()
        PortalToolLink.objects.update_or_create(
            url_hash=url_hash,
            defaults={
                **item,
                'category': 'mythic',
                'badge': '',
                'badge_tone': 'default',
                'source': 'wowdaily',
                'sort_order': item['topbar_order'],
                'is_topbar': False,
                'show_in_guide': True,
                'show_in_tools': False,
                'open_in_new_tab': False,
                'icon_path': '',
                'is_active': True,
            },
        )


def restore_home_guide(apps, schema_editor):
    PortalToolLink = apps.get_model('botend', 'PortalToolLink')
    PortalToolLink.objects.filter(
        url__in=('/#section-news', '/#section-events'),
    ).update(show_in_guide=True)
    PortalToolLink.objects.filter(url='/#section-wow-skill-diff').update(
        category='data',
        topbar_order=55,
        sort_order=55,
    )
    PortalToolLink.objects.filter(
        source='wowdaily',
        url__in=tuple(item['url'] for item in MYTHIC_GUIDE_LINKS),
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0196_portal_tool_link_guide_badge_tone'),
    ]

    operations = [
        migrations.RunPython(configure_home_guide, restore_home_guide),
    ]
