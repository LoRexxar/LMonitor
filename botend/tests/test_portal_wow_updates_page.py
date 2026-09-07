from importlib import import_module
from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from botend.models import PortalNavigationGroup, PortalNavigationItem


ROOT = Path(__file__).resolve().parents[2]


class PortalWowUpdatesPageTests(SimpleTestCase):
    def test_standalone_page_is_public_and_preserves_content_containers(self):
        response = self.client.get(reverse('portal_wow_updates'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/wow_updates.html')
        for text in ('魔兽世界更新数据挖掘', 'wow-skill-diff-states', 'wow-skill-diff-list', 'wow-updates-search'):
            self.assertContains(response, text)
        self.assertNotContains(response, "portal/js/main.js")

    def test_home_removes_section_and_requests_but_keeps_legacy_redirect(self):
        response = self.client.get('/')
        self.assertNotContains(response, 'id="section-wow-skill-diff"')
        source = (ROOT / 'static/portal/js/main.js').read_text(encoding='utf-8')
        self.assertNotIn('/portal/api/wow-skill', source)
        self.assertNotIn('renderWowSkillDiff', source)
        self.assertIn('window.location.hash === "#section-wow-skill-diff"', source)
        self.assertIn('window.location.replace("/portal/wow-updates/")', source)


class PortalWowUpdatesNavigationTests(TestCase):
    def test_migration_is_reversible_and_preserves_custom_navigation(self):
        migration = import_module('botend.migrations.0205_move_wow_updates_navigation')
        group = PortalNavigationGroup.objects.create(key='updates-test', name='测试')
        item = PortalNavigationItem.objects.create(
            group=group, name='自定义版本入口', url=migration.OLD_URL,
            desc='保留说明', sort_order=7, is_active=False,
        )
        migration.move_navigation(apps, None)
        migration.move_navigation(apps, None)
        item.refresh_from_db()
        self.assertEqual(item.url, migration.NEW_URL)
        self.assertEqual((item.name, item.desc, item.sort_order, item.is_active), ('自定义版本入口', '保留说明', 7, False))
        migration.restore_navigation(apps, None)
        item.refresh_from_db()
        self.assertEqual(item.url, migration.OLD_URL)
