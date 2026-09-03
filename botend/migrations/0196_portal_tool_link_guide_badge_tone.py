import hashlib

from django.db import migrations, models


HOME_GUIDE_LINKS = (
    {
        'name': '版本改动',
        'url': '/#section-wow-skill-diff',
        'desc': '查看当前版本的数据挖掘与职业改动',
        'category': 'data',
        'icon_key': 'refresh',
        'badge': '新',
        'badge_tone': 'new',
        'topbar_order': 55,
    },
    {
        'name': '公开模拟任务',
        'url': '/#section-simc-baselines',
        'desc': '查看首页公开的职业模拟任务',
        'category': 'data',
        'icon_key': 'chart',
        'topbar_order': 65,
    },
    {
        'name': '快捷链接',
        'url': '/#section-tools',
        'desc': '跳转到首页常用工具与站点目录',
        'category': 'tools',
        'icon_key': 'tools',
        'topbar_order': 75,
    },
)


def seed_home_guide_links(apps, schema_editor):
    PortalToolLink = apps.get_model('botend', 'PortalToolLink')
    PortalToolLink.objects.filter(is_topbar=True).update(show_in_guide=True)

    for item in HOME_GUIDE_LINKS:
        url_hash = hashlib.sha256(item['url'].encode('utf-8')).hexdigest()
        PortalToolLink.objects.get_or_create(
            url_hash=url_hash,
            defaults={
                **item,
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


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0195_portal_tool_link_navigation'),
    ]

    operations = [
        migrations.AddField(
            model_name='portaltoollink',
            name='badge_tone',
            field=models.CharField(
                choices=[('default', '常规'), ('new', '新内容（红色）')],
                default='default',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='portaltoollink',
            name='show_in_guide',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(seed_home_guide_links, migrations.RunPython.noop),
    ]
