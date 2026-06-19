from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from botend.portal.views import PortalWowSkillDiffReportView


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

    def test_missing_html_report_falls_back_to_markdown(self):
        report = SimpleNamespace(
            id=1,
            branch='wowxptr',
            from_build='12.0.7.67360',
            to_build='12.0.7.67525',
            display_from_build='',
            display_to_build='',
            content_md='# 牧师技能更新（3项）\n\n- 技能数：3\n',
            content_html_path='portal/reports/not_exists_for_test.html',
            created_at=None,
        )

        response = self._get(report)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('HTML 报告文件不存在，已回退显示 Markdown 记录。', html)
        self.assertIn('wow-skill-diff-md', html)
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
            created_at=None,
        )

        response = self._get(report)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('<iframe', html)
        self.assertNotIn('HTML 报告文件不存在', html)
