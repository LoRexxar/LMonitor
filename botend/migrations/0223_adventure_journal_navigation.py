"""将冒险手册注册为独立导航板块，并接入每日同步监控。"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def register_journal(apps, schema_editor):
    alias = schema_editor.connection.alias
    Group = apps.get_model('botend', 'PortalNavigationGroup')
    Item = apps.get_model('botend', 'PortalNavigationItem')
    Task = apps.get_model('botend', 'MonitorTask')
    group, _ = Group.objects.using(alias).get_or_create(key='adventure-journal', defaults={
        'name': '冒险手册', 'description': '副本、首领、职责提示与掉落', 'icon_key': 'globe', 'sort_order': 25})
    Item.objects.using(alias).get_or_create(url='/portal/adventure-journal/', defaults={
        'group': group, 'name': '冒险手册', 'desc': '全资料片副本首领、技能、职责提示和掉落查询',
        'icon_key': 'globe', 'badge': '副本百科', 'badge_tone': 'default', 'is_active': True})
    Task.objects.using(alias).get_or_create(name='AdventureJournalMonitor', defaults={
        'type': 35, 'target': 'https://wago.tools/journal', 'is_active': True,
        'wait_time': 86400, 'last_scan_time': timezone.now() - timedelta(days=2)})


class Migration(migrations.Migration):
    dependencies = [('botend', '0222_adventure_journal')]
    operations = [migrations.RunPython(register_journal, migrations.RunPython.noop)]
