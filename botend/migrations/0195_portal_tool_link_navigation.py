import hashlib

from django.db import migrations, models


DEFAULT_NAVIGATION = (
    {
        'name': '今日内容',
        'url': '/#section-news',
        'desc': '今日魔兽、蓝帖与最新资讯',
        'category': 'today',
        'icon_key': 'calendar',
        'topbar_order': 10,
    },
    {
        'name': '活动提醒',
        'url': '/#section-events',
        'desc': '游戏活动、世界事件与时间节点',
        'category': 'today',
        'icon_key': 'calendar',
        'topbar_order': 20,
    },
    {
        'name': '新闻聚合',
        'url': '/portal/news/',
        'desc': '蓝帖、Wowhead 与社区资讯',
        'category': 'community',
        'icon_key': 'newspaper',
        'topbar_order': 30,
    },
    {
        'name': 'NGA 热议',
        'url': '/#section-nga',
        'desc': '社区正在讨论的热门内容',
        'category': 'community',
        'icon_key': 'chat',
        'topbar_order': 40,
    },
    {
        'name': '视频攻略',
        'url': '/#section-videos',
        'desc': '近期职业、副本与玩法视频',
        'category': 'community',
        'icon_key': 'video',
        'topbar_order': 50,
    },
    {
        'name': '全职业数据',
        'url': '/portal/specs/',
        'desc': '专精排名、人物榜与副本数据',
        'category': 'data',
        'icon_key': 'chart',
        'topbar_order': 60,
    },
    {
        'name': 'SimC 模拟数据',
        'url': '/portal/simc-benchmarks/',
        'desc': '公开基线与职业模拟结果',
        'category': 'data',
        'icon_key': 'refresh',
        'topbar_order': 70,
    },
    {
        'name': '天赋模拟器',
        'url': '/portal/talents/',
        'desc': '导入、编辑并导出天赋字符串',
        'category': 'tools',
        'icon_key': 'chart',
        'topbar_order': 80,
    },
    {
        'name': '职业配装器',
        'url': '/portal/gear-builder/',
        'desc': '搭配装备、宝石、附魔并计算属性',
        'category': 'tools',
        'icon_key': 'tools',
        'badge': '常用',
        'topbar_order': 90,
    },
    {
        'name': 'MDT 路线',
        'url': '/portal/mythic-planner/',
        'desc': '查看和规划大秘境路线',
        'category': 'tools',
        'icon_key': 'tools',
        'topbar_order': 100,
    },
)


def seed_navigation(apps, schema_editor):
    PortalToolLink = apps.get_model('botend', 'PortalToolLink')
    for item in DEFAULT_NAVIGATION:
        url_hash = hashlib.sha256(item['url'].encode('utf-8')).hexdigest()
        PortalToolLink.objects.get_or_create(
            url_hash=url_hash,
            defaults={
                **item,
                'source': 'wowdaily',
                'sort_order': item['topbar_order'],
                'is_topbar': True,
                'show_in_tools': item['category'] == 'tools',
                'open_in_new_tab': False,
                'icon_path': '',
                'is_active': True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0194_wowtodaycardsetting_wowtodaycardsnapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='portaltoollink',
            name='badge',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='portaltoollink',
            name='category',
            field=models.CharField(default='tools', max_length=32),
        ),
        migrations.AddField(
            model_name='portaltoollink',
            name='icon_key',
            field=models.CharField(blank=True, default='', max_length=48),
        ),
        migrations.AddField(
            model_name='portaltoollink',
            name='open_in_new_tab',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='portaltoollink',
            name='show_in_tools',
            field=models.BooleanField(default=True),
        ),
        migrations.AddIndex(
            model_name='portaltoollink',
            index=models.Index(fields=['category', 'is_active'], name='wow_portal__categor_47e1b6_idx'),
        ),
        migrations.RunPython(seed_navigation, migrations.RunPython.noop),
    ]
