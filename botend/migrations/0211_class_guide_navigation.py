"""将全职业攻略加入 Portal 统一导航，顶部菜单和首页共用。"""
from django.db import migrations


def add_guide_navigation(apps, schema_editor):
    Group = apps.get_model('botend', 'PortalNavigationGroup')
    Item = apps.get_model('botend', 'PortalNavigationItem')
    alias = schema_editor.connection.alias
    # 已手工配置的入口继续由后台管理，重复执行不覆盖配置。
    if Item.objects.using(alias).filter(url='/portal/class-guides/').exists():
        return
    group, _ = Group.objects.using(alias).get_or_create(key='data', defaults={
        'name': '数据中心', 'description': '职业攻略与实战数据',
        'icon_key': 'chart', 'sort_order': 20,
    })
    Item.objects.using(alias).create(
        group=group, name='全职业攻略', url='/portal/class-guides/',
        desc='全职业专精攻略、天赋、配装与实战循环',
        icon_key='newspaper', badge='内部预览', badge_tone='default',
        sort_order=61, is_active=True,
    )


class Migration(migrations.Migration):
    dependencies = [('botend', '0210_class_guide_specialization')]
    operations = [migrations.RunPython(add_guide_navigation, migrations.RunPython.noop)]
