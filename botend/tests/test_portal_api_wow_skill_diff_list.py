import json
from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from django.test import RequestFactory

from botend.models import WowSkillDiffReport
from botend.portal.api import PortalWowSkillDiffListAPIView


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalWowSkillDiffListAPIViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view = PortalWowSkillDiffListAPIView.as_view()

    def test_filters_reports_older_than_two_months(self):
        """Only reports created within the last 2 months should be returned"""
        now = timezone.now()
        two_months_ago = now - timedelta(days=60)
        three_months_ago = now - timedelta(days=90)

        recent = WowSkillDiffReport.objects.create(
            branch='wow',
            locale='enUS',
            from_build='11.0.7.57212',
            to_build='11.0.7.57291',
        )
        WowSkillDiffReport.objects.filter(id=recent.id).update(created_at=now - timedelta(days=30))
        recent.refresh_from_db()

        edge_case = WowSkillDiffReport.objects.create(
            branch='wow',
            locale='enUS',
            from_build='11.0.7.57100',
            to_build='11.0.7.57200',
        )
        WowSkillDiffReport.objects.filter(id=edge_case.id).update(created_at=two_months_ago + timedelta(hours=1))
        edge_case.refresh_from_db()

        old = WowSkillDiffReport.objects.create(
            branch='wow',
            locale='enUS',
            from_build='11.0.7.56000',
            to_build='11.0.7.56100',
        )
        WowSkillDiffReport.objects.filter(id=old.id).update(created_at=three_months_ago)
        old.refresh_from_db()

        request = self.factory.get('/portal/api/wow-skill-diffs/')
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['status'], 'success')

        returned_ids = {item['id'] for item in data['data']}
        self.assertIn(recent.id, returned_ids)
        self.assertIn(edge_case.id, returned_ids)
        self.assertNotIn(old.id, returned_ids)

    def test_respects_limit_parameter(self):
        """The limit parameter should still work with the 2-month filter"""
        now = timezone.now()
        for i in range(5):
            report = WowSkillDiffReport.objects.create(
                branch='wow',
                locale='enUS',
                from_build=f'11.0.7.5700{i}',
                to_build=f'11.0.7.5710{i}',
            )
            WowSkillDiffReport.objects.filter(id=report.id).update(created_at=now - timedelta(days=i))

        request = self.factory.get('/portal/api/wow-skill-diffs/?limit=3')
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data['data']), 3)

    def test_returns_empty_when_no_recent_reports(self):
        """Should return empty list when all reports are older than 2 months"""
        three_months_ago = timezone.now() - timedelta(days=90)
        report = WowSkillDiffReport.objects.create(
            branch='wow',
            locale='enUS',
            from_build='11.0.7.56000',
            to_build='11.0.7.56100',
        )
        WowSkillDiffReport.objects.filter(id=report.id).update(created_at=three_months_ago)

        request = self.factory.get('/portal/api/wow-skill-diffs/')
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['status'], 'success')
        self.assertEqual(len(data['data']), 0)

    def test_orders_by_created_at_descending(self):
        """Results should be ordered by created_at in descending order"""
        now = timezone.now()
        oldest_recent = WowSkillDiffReport.objects.create(
            branch='wow',
            locale='enUS',
            from_build='11.0.7.57000',
            to_build='11.0.7.57100',
        )
        WowSkillDiffReport.objects.filter(id=oldest_recent.id).update(created_at=now - timedelta(days=50))
        oldest_recent.refresh_from_db()

        middle = WowSkillDiffReport.objects.create(
            branch='wow',
            locale='enUS',
            from_build='11.0.7.57100',
            to_build='11.0.7.57200',
        )
        WowSkillDiffReport.objects.filter(id=middle.id).update(created_at=now - timedelta(days=25))
        middle.refresh_from_db()

        newest = WowSkillDiffReport.objects.create(
            branch='wow',
            locale='enUS',
            from_build='11.0.7.57200',
            to_build='11.0.7.57300',
        )
        WowSkillDiffReport.objects.filter(id=newest.id).update(created_at=now - timedelta(days=1))
        newest.refresh_from_db()

        request = self.factory.get('/portal/api/wow-skill-diffs/')
        response = self.view(request)

        data = json.loads(response.content)
        returned_ids = [item['id'] for item in data['data']]
        self.assertEqual(returned_ids, [newest.id, middle.id, oldest_recent.id])
