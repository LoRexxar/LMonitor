"""调整首页与顶栏的四组入口，为同一小标题下的链接保留独立地址。"""

from django.db import migrations, models


def regroup(apps, schema_editor):
    db = schema_editor.connection.alias
    groups = apps.get_model('botend', 'PortalNavigationGroup').objects.using(db)
    items = apps.get_model('botend', 'PortalNavigationItem').objects.using(db)
    sections = [
        ('data', '职业与实战', '职业攻略、专精表现与实战排行', 'chart', [
            '/portal/class-guides/', '/portal/specs/', '/portal/mplus/dps-rankings/',
            '/#section-peak-spec', '/portal/rio-statistics/', '/portal/mplus/dps-rankings/?source=mythicstats',
        ]),
        ('tools', '模拟与配装', '专精方案、装备对比与配装工具', 'tools', [
            '/portal/simc-reference/', '/portal/gear-builder/', '/portal/talents/',
            '/portal/simc-benchmarks/', '/portal/simc-benchmarks/8/', '/portal/simc-benchmarks/30/',
        ]),
        ('mythic', '副本与路线', '首领机制、路线规划与限时成绩', 'refresh', [
            '/portal/adventure-journal/', '/portal/mythic-planner/', '/#section-mplus-cutoffs', '/#section-rank',
        ]),
        ('community', '资讯与社区', '版本改动、新闻与玩家讨论', 'newspaper', [
            '/portal/wow-updates/', '/portal/news/', '/portal/nga/', '/#section-videos',
        ]),
    ]
    labels = {
        '/portal/simc-benchmarks/': {'name': '模拟专题', 'desc': '查看全部装备模拟与历期结果'},
        '/portal/simc-benchmarks/8/': {'subheading': '12.1 赛季', 'name': '饰品对比'},
        '/portal/simc-benchmarks/30/': {'subheading': '12.1 赛季', 'name': '非饰品特效装备对比'},
    }
    for pos, (key, name, description, icon, urls) in enumerate(sections, 1):
        group, _ = groups.update_or_create(key=key, defaults={
            'name': name, 'description': description, 'icon_key': icon, 'sort_order': pos * 10,
        })
        for rank, url in enumerate(urls, 1):
            # 面板编号对应线上已配置的专题；没有该入口的站点不凭编号创建链接。
            rows = items.filter(url=url, group__key__in=('data', 'tools', 'mythic', 'community'))
            if url == '/portal/simc-reference/' and not items.filter(url=url).exists():
                items.create(group=group, url=url, name='专精模拟资料', desc='按专精查看天赋、装备、APL 和模拟排名',
                             icon_key='chart', sort_order=rank * 10)
            else:
                rows.update(group=group, sort_order=rank * 10, **labels.get(url, {}))


class Migration(migrations.Migration):
    dependencies = [('botend', '0281_dungeon_character_fights')]
    operations = [
        migrations.AddField(model_name='portalnavigationitem', name='subheading',
                            field=models.CharField(max_length=64, default='', blank=True)),
        migrations.RunPython(regroup, migrations.RunPython.noop),
    ]
