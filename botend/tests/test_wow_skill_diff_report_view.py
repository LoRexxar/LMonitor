from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from botend.portal.views import PortalWowSkillDiffReportView
from botend.services.wago_report_html import build_wow_skill_diff_fallback_html


class _FakeQuerySet:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _FakeManager:
    def __init__(self, row):
        self.row = row

    def filter(self, **kwargs):
        return _FakeQuerySet(self.row)


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalWowSkillDiffReportViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _get(self, report):
        request = self.factory.get(f'/portal/wow-skill-diff/{report.id}/')
        with patch('botend.portal.views.WowSkillDiffReport.objects', _FakeManager(report)):
            return PortalWowSkillDiffReportView.as_view()(request, report_id=report.id)

    def test_missing_html_report_renders_inline_html_summary(self):
        report = SimpleNamespace(
            id=1,
            branch='wowxptr',
            from_build='12.0.7.67360',
            to_build='12.0.7.67525',
            display_from_build='',
            display_to_build='',
            content_md='# 牧师技能更新（3项）\n\n- 技能数：3\n',
            content_html_path='portal/reports/not_exists_for_test.html',
            changed_tables_json='["SpellEffect", "SpellName"]',
            spell_count=3,
            class_count=1,
            created_at=None,
        )

        response = self._get(report)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('HTML 报告文件不存在，已直接生成 HTML 摘要视图。', html)
        self.assertIn('wow-skill-diff-fallback-html', html)
        self.assertIn('SpellEffect', html)
        self.assertNotIn('wow-skill-diff-md', html)
        self.assertNotIn('marked.min.js', html)
        self.assertNotIn('<iframe', html)

    def test_existing_html_report_uses_iframe(self):
        report = SimpleNamespace(
            id=2,
            branch='wowt',
            from_build='12.0.5.67186',
            to_build='12.0.5.67235',
            display_from_build='',
            display_to_build='',
            content_md='# PTR(测试服) 职业技能变更报告\n',
            content_html_path='portal/reports/wow_skill_diff_wowt_enUS_12_0_5_67235.html',
            changed_tables_json='[]',
            spell_count=0,
            class_count=0,
            created_at=None,
        )

        response = self._get(report)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('<iframe', html)
        self.assertNotIn('HTML 报告文件不存在', html)

    def test_inline_html_summary_escapes_report_values(self):
        report = SimpleNamespace(
            id=3,
            branch='wowt',
            from_build='<script>alert(1)</script>',
            to_build='12.0.5.67235',
            display_from_build='',
            display_to_build='',
            content_md='# <img src=x onerror=alert(1)>\n\n- 技能数：3\n',
            content_html_path='',
            changed_tables_json='["Spell<script>"]',
            spell_count=3,
            class_count=1,
        )

        html = build_wow_skill_diff_fallback_html(report, page_title='<b>bad</b>', server_title='PTR')

        self.assertIn('&lt;b&gt;bad&lt;/b&gt;', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('Spell&lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertNotIn('<img src=x', html)

    def test_inline_html_summary_wraps_long_table_names(self):
        report = SimpleNamespace(
            id=4,
            branch='wowt',
            from_build='12.1.0.68209',
            to_build='12.1.0.68301',
            display_from_build='',
            display_to_build='',
            content_md='# PTR(测试服) 职业技能变更报告\n',
            content_html_path='',
            changed_tables_json='["collectablesourcevendorsparse", "creaturedisplayinfogeosetdata"]',
            spell_count=117,
            class_count=12,
        )

        html = build_wow_skill_diff_fallback_html(report, page_title='', server_title='PTR')

        self.assertIn('collectablesourcevendorsparse', html)
        self.assertIn('creaturedisplayinfogeosetdata', html)
        self.assertIn('overflow-wrap:anywhere', html)
        self.assertIn('word-break:break-word', html)
        self.assertIn('minmax(220px,1fr)', html)
