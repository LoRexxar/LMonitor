import hashlib

from django.db import migrations


GROUP_UPDATES = {
    'today': (
        '今日动态', '今日速览',
        '重置、活动与当天重点', '当天内容、活动与时间节点',
    ),
    'data': (
        '职业与数据', '数据中心',
        '专精页面与模拟结果', '职业、模拟与公开数据',
    ),
    'mythic': (
        '大秘境数据', '大秘境',
        '分数线、巅峰榜与热门排行', '分数线、排行榜与专精表现',
    ),
    'tools': (
        '玩家工具', '站内工具',
        '天赋、配装与路线规划', '天赋、配装与路线规划',
    ),
    'community': (
        '内容社区', '资讯社区',
        '新闻、讨论与视频攻略', '新闻、版本改动与玩家内容',
    ),
}

DEFAULT_EXTERNAL_LINKS = (
    ('Wowhead', 'https://www.wowhead.com/', '魔兽资料、数据库与新闻', 'community', 'newspaper', 10),
    ('Raider.IO', 'https://raider.io/', '大秘境排行与角色数据', 'mythic', 'refresh', 20),
    ('Warcraft Logs', 'https://www.warcraftlogs.com/', '战斗日志与团队分析', 'data', 'chart', 30),
    ('Raidbots', 'https://www.raidbots.com/simbot', '在线角色模拟工具', 'tools', 'tools', 40),
)


def refine_navigation(apps, schema_editor):
    PortalNavigationGroup = apps.get_model('botend', 'PortalNavigationGroup')
    PortalNavigationItem = apps.get_model('botend', 'PortalNavigationItem')
    PortalToolLink = apps.get_model('botend', 'PortalToolLink')
    for key, (old_name, new_name, old_desc, new_desc) in GROUP_UPDATES.items():
        PortalNavigationGroup.objects.filter(key=key, name=old_name).update(name=new_name)
        PortalNavigationGroup.objects.filter(key=key, description=old_desc).update(description=new_desc)
    PortalNavigationItem.objects.filter(url='/#section-tools').update(show_in_home_guide=False)
    for name, url, desc, category, icon_key, sort_order in DEFAULT_EXTERNAL_LINKS:
        PortalToolLink.objects.get_or_create(
            url_hash=hashlib.sha256(url.encode('utf-8')).hexdigest(),
            defaults={
                'name': name,
                'url': url,
                'desc': desc,
                'source': 'wowdaily-default',
                'category': category,
                'icon_key': icon_key,
                'badge': '',
                'badge_tone': 'default',
                'sort_order': sort_order,
                'is_topbar': False,
                'topbar_order': 0,
                'show_in_guide': False,
                'show_in_tools': True,
                'open_in_new_tab': True,
                'icon_path': '',
                'is_active': True,
            },
        )


def restore_navigation(apps, schema_editor):
    PortalNavigationGroup = apps.get_model('botend', 'PortalNavigationGroup')
    PortalNavigationItem = apps.get_model('botend', 'PortalNavigationItem')
    PortalToolLink = apps.get_model('botend', 'PortalToolLink')
    for key, (old_name, new_name, old_desc, new_desc) in GROUP_UPDATES.items():
        PortalNavigationGroup.objects.filter(key=key, name=new_name).update(name=old_name)
        PortalNavigationGroup.objects.filter(key=key, description=new_desc).update(description=old_desc)
    PortalNavigationItem.objects.filter(url='/#section-tools').update(show_in_home_guide=True)
    PortalToolLink.objects.filter(source='wowdaily-default').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0198_split_portal_navigation'),
    ]

    operations = [
        migrations.RunPython(refine_navigation, restore_navigation),
    ]
