"""Fixed publication-day entry, without rewriting historical report routes."""
import tempfile
from datetime import timedelta
from pathlib import Path
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from botend.models import WowDailyReport
from botend.wow_daily_report.site_sections import REPORT_ZONE, day_window


class CurrentDailyEntryTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        override = override_settings(BASE_DIR=self.root.name)
        override.enable();self.addCleanup(override.disable)
        self.today = timezone.localdate(timezone=REPORT_ZONE)
        self.start, self.end = day_window(self.today)
        self.url = '/portal/daily-report/'

    def edition(self, report_date, text, updated_at=None):
        path = f'portal/reports/wow_daily_report_{report_date.isoformat()}.html'
        target = Path(self.root.name)/'static'/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'<!doctype html><html><body>{text}</body></html>')
        row = WowDailyReport.objects.create(report_date=report_date, md_path=path)
        if updated_at is not None:
            WowDailyReport.objects.filter(pk=row.pk).update(updated_at=updated_at)
        return row, target

    def test_fixed_route_serves_newest_edition_published_today_without_redirect(self):
        self.edition(self.today-timedelta(days=2), 'older-today')
        row, _ = self.edition(self.today-timedelta(days=1), 'current-edition')
        self.edition(self.today+timedelta(days=1), 'future-edition')
        before = list(WowDailyReport.objects.order_by('pk').values())
        response = self.client.get(self.url)
        self.assertEqual(reverse('portal_daily_report_current'), self.url)
        self.assertContains(response, 'current-edition')
        self.assertNotIn('Location', response)
        self.assertEqual(response['X-Daily-Report-ID'], str(row.pk))
        self.assertEqual(list(WowDailyReport.objects.order_by('pk').values()), before)

    def test_no_today_publication_shows_pending_not_an_old_edition(self):
        row, _ = self.edition(self.today-timedelta(days=1), 'old-edition', self.start-timedelta(microseconds=1))
        response = self.client.get(self.url)
        self.assertContains(response, '今日日报待发布')
        self.assertNotContains(response, 'old-edition')
        old = self.client.get('/'+row.md_path)
        self.assertContains(old, 'old-edition')
        self.assertNotIn('Location', old)

    def test_empty_database_and_date_parameters_cannot_select_history(self):
        self.assertContains(self.client.get(self.url), '今日日报待发布')
        self.edition(self.today-timedelta(days=2), 'old-edition', self.start-timedelta(seconds=1))
        self.edition(self.today-timedelta(days=1), 'current-edition')
        response = self.client.get(self.url, {'date': '2000-01-01', 'report_id': '1', 'generate': 'true'})
        self.assertContains(response, 'current-edition')
        self.assertNotContains(response, 'old-edition')
        self.assertEqual(WowDailyReport.objects.count(), 2)

    def test_shanghai_publication_window_is_start_inclusive_end_exclusive(self):
        self.edition(self.today-timedelta(days=2), 'midnight-edition', self.start)
        self.edition(self.today-timedelta(days=1), 'tomorrow-edition', self.end)
        self.assertContains(self.client.get(self.url), 'midnight-edition')
        self.assertNotContains(self.client.get(self.url), 'tomorrow-edition')

    def test_missing_latest_artifact_does_not_fall_back_to_other_reports(self):
        self.edition(self.today-timedelta(days=2), 'older-today')
        _, path = self.edition(self.today-timedelta(days=1), 'missing-latest')
        path.unlink()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, 'older-today', status_code=503)

    def test_artifact_path_guard_and_cache_headers_apply_to_the_fixed_entry(self):
        row, path = self.edition(self.today-timedelta(days=1), 'first-version')
        first = self.client.get(self.url)
        self.assertIn('no-store', first['Cache-Control'])
        self.assertIn('no-cache', first['Cache-Control'])
        path.write_text('<!doctype html><html><body>second-version</body></html>')
        self.assertContains(self.client.get(self.url), 'second-version')
        outside = Path(self.root.name)/'secret.html';outside.write_text('private-content')
        WowDailyReport.objects.filter(pk=row.pk).update(md_path=str(outside))
        failed = self.client.get(self.url)
        self.assertEqual(failed.status_code, 503)
        self.assertNotContains(failed, 'private-content', status_code=503)

    def test_unreadable_report_directory_returns_uncached_unavailable(self):
        _, path = self.edition(self.today-timedelta(days=1), 'inaccessible-edition')
        path.parent.chmod(0)
        try:
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 503)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, 'inaccessible-edition', status_code=503)
        finally:
            path.parent.chmod(0o700)

    def test_symlink_loop_returns_uncached_unavailable(self):
        row, path = self.edition(self.today-timedelta(days=1), 'loop-edition')
        path.unlink()
        path.symlink_to(path.name)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertNotContains(response, 'loop-edition', status_code=503)

    def test_latest_api_adds_fixed_entry_without_changing_dated_url(self):
        row, _ = self.edition(self.today-timedelta(days=1), 'current-edition')
        data = self.client.get('/portal/api/daily-report/latest/').json()['data']
        self.assertEqual(data['current_url'], self.url)
        self.assertTrue(data['url'].startswith('/'+row.md_path+'?v='))
        self.assertEqual(data['md_path'], row.md_path)
