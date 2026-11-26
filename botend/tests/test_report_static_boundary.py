from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.management import call_command, CommandError
from django.test import RequestFactory, SimpleTestCase, override_settings

from botend.dashboard.dashboard import WclAnalysisReportView
from botend.report_static import ALIAS, PRIVATE, PUBLIC


class ReportStaticBoundaryTests(SimpleTestCase):
    def test_collect_public_alias_not_private_reports_or_stale_copies(self):
        self.assertIn('botend.report_static.ReportSafeFinder', settings.STATICFILES_FINDERS)
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            src, dest = base / 'static', base / 'staticfiles'
            files = [f'{PUBLIC}/archive/example.html', f'{PUBLIC}/state.json',
                     *(f'{group}/private.html' for group in PRIVATE), 'portal/js/chart.js']
            for root in (src, dest):
                for name in files:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('original bytes')
            with override_settings(BASE_DIR=base, STATIC_ROOT=dest, STATICFILES_DIRS=[src],
                                   STATICFILES_FINDERS=['botend.report_static.ReportSafeFinder']):
                call_command('clear_collected_report_copies')
                call_command('collectstatic', interactive=False, verbosity=0)
                self.assertEqual((dest / files[0]).read_bytes(), ALIAS.read_bytes())
                self.assertEqual(Path(finders.find(files[0])), ALIAS)
                for name in files[1:-1]:
                    self.assertFalse(finders.find(name))
                    self.assertFalse((dest / name).exists())
                self.assertEqual((dest / files[-1]).read_text(), 'original bytes')
                self.assertTrue(all((src / name).read_text() == 'original bytes' for name in files))
                with override_settings(STATIC_ROOT=src), self.assertRaises(CommandError):
                    call_command('clear_collected_report_copies')

    def test_wcl_token_view_projects_only_display(self):
        with TemporaryDirectory() as tmp, override_settings(BASE_DIR=tmp):
            path = Path(tmp, 'static/wcl_reports/report.html')
            path.parent.mkdir(parents=True)
            origin = 'https://img.example.org/image.png'
            original = f'<img src="{origin}"><canvas id="chart1"></canvas><script>draw()</script>'
            path.write_text(original)
            task = SimpleNamespace(id=1, access_token='token', status=2, error_message='', report_html_file=path.name)
            with patch('botend.dashboard.dashboard.WclAnalysisTask.objects.filter') as query:
                query.return_value.first.return_value = task
                view = WclAnalysisReportView.as_view()
                self.assertEqual(view(RequestFactory().get('/'), 1).status_code, 403)
                response = view(RequestFactory().get('/', {'token':'token'}), 1)
            self.assertContains(response, '<canvas id="chart1"></canvas><script>draw()</script>')
            self.assertNotContains(response, origin)
            self.assertEqual(response['Cache-Control'], 'private, no-store')
            self.assertEqual(response['Referrer-Policy'], 'no-referrer')
            self.assertEqual(path.read_text(), original)
