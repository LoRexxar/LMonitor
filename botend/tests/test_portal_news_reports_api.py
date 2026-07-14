import json
from datetime import timedelta

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from botend.models import WowHotfixReport, WowSkillDiffReport
from botend.portal.api import PortalHotfixReportsAPIView, PortalWowSkillDiffListAPIView


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalNewsReportsAPIViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @staticmethod
    def _set_created_at(model, row, value):
        model.objects.filter(id=row.id).update(created_at=value)

    def test_build_reports_paginate_recent_rows_and_exclude_older_rows(self):
        now = timezone.now()
        recent = []
        for index in range(3):
            row = WowSkillDiffReport.objects.create(
                branch='wow',
                locale='enUS',
                from_build=f'7000{index}',
                to_build=f'7001{index}',
            )
            self._set_created_at(WowSkillDiffReport, row, now - timedelta(days=index))
            recent.append(row)
        old = WowSkillDiffReport.objects.create(
            branch='wow', locale='enUS', from_build='60000', to_build='60001'
        )
        self._set_created_at(WowSkillDiffReport, old, now - timedelta(days=61))

        response = PortalWowSkillDiffListAPIView.as_view()(
            self.factory.get('/portal/api/wow-skill-diffs/?page=2&page_size=2')
        )
        payload = json.loads(response.content)

        self.assertEqual([item['id'] for item in payload['data']], [recent[2].id])
        self.assertEqual(payload['meta']['total'], 3)
        self.assertEqual(payload['meta']['page'], 2)
        self.assertEqual(payload['meta']['total_pages'], 2)
        self.assertTrue(payload['meta']['has_previous'])
        self.assertFalse(payload['meta']['has_next'])
        self.assertNotIn(old.id, {item['id'] for item in payload['data']})

    def test_hotfix_reports_paginate_recent_rows_and_use_existing_report_route(self):
        now = timezone.now()
        recent = []
        for index in range(3):
            row = WowHotfixReport.objects.create(
                branch='wow',
                locale='enUS',
                build_num=f'7000{index}',
                from_push=100 + index,
                to_push=101 + index,
                summary_title=f'Hotfix {index}',
            )
            self._set_created_at(WowHotfixReport, row, now - timedelta(days=index))
            recent.append(row)
        old = WowHotfixReport.objects.create(
            branch='wow', locale='enUS', from_push=1, to_push=2, summary_title='old'
        )
        self._set_created_at(WowHotfixReport, old, now - timedelta(days=61))

        response = PortalHotfixReportsAPIView.as_view()(
            self.factory.get('/portal/api/hotfix-reports/?page=2&page_size=2')
        )
        payload = json.loads(response.content)

        self.assertEqual([item['id'] for item in payload['data']], [recent[2].id])
        self.assertEqual(payload['data'][0]['url'], f'/portal/wow-hotfix-report/{recent[2].id}/')
        self.assertEqual(payload['data'][0]['title'].count('push 102→103'), 1)
        self.assertEqual(payload['meta']['total'], 3)
        self.assertEqual(payload['meta']['page'], 2)
        self.assertEqual(payload['meta']['total_pages'], 2)
        self.assertNotIn(old.id, {item['id'] for item in payload['data']})

    def test_news_page_defaults_to_news_and_declares_lazy_report_tabs(self):
        response = self.client.get('/portal/news/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('data-news-tab="news"', html)
        self.assertIn('data-news-tab="build"', html)
        self.assertIn('data-news-tab="hotfix"', html)
        self.assertIn('aria-selected="true"', html)
        self.assertIn('新闻资讯', html)
