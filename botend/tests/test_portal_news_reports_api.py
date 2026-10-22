"""Portal report lists: typed provenance, bounded history, and archive pagination."""
import json
from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from django.test import RequestFactory
from botend.models import WowHotfixReport, WowSkillDiffReport
from botend.portal.api import PortalWowSkillDiffListAPIView
from pathlib import Path
from botend.portal.api import PortalHotfixReportsAPIView

class ReportRequestFixture:
    """Common request preparation without discoverable inherited tests."""
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.view = PortalWowSkillDiffListAPIView.as_view()


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalWowSkillDiffListAPIViewTests(ReportRequestFixture, TestCase):

    def test_build_and_verified_hotfix_class_reports_keep_distinct_typed_urls(self):
        build = WowSkillDiffReport.objects.create(
            branch='wow', locale='enUS', from_build='12.1.0.69814', to_build='12.1.0.69875',
        )
        hotfix = WowHotfixReport.objects.create(
            branch='wow', locale='enUS', region_id=1, from_push=112181, to_push=112185,
            build_str='12.1.0.69933', collection_complete=True,
            class_content_html_path='portal/reports/wow_skill_diff_wow_enUS_hotfix_r1_p112185.html',
            class_spell_count=2, class_class_count=1, class_unresolved_count=2,
        )
        incomplete = WowHotfixReport.objects.create(
            branch='wow', locale='enUS', region_id=3, to_push=112185,
            class_content_html_path='portal/reports/unverified.html', class_spell_count=1,
        )
        response = self.view(self.factory.get('/portal/api/wow-skill-diffs/?page_size=10'))
        payload = json.loads(response.content)
        self.assertEqual(payload['meta']['total'], 2)
        self.assertEqual({entry['url'] for entry in payload['data']}, {
            f'/portal/wow-skill-diff/{build.id}/', f'/portal/wow-hotfix-class/{hotfix.id}/',
        })
        self.assertEqual({entry['type'] for entry in payload['data']}, {'build', 'hotfix'})
        hotfix_entry = next(entry for entry in payload['data'] if entry['type'] == 'hotfix')
        self.assertEqual(hotfix_entry['unresolved_count'], 2)
        self.assertIn('2 条失效来源归属未核实', hotfix_entry['title'])
        self.assertNotIn(f'/portal/wow-hotfix-class/{incomplete.id}/', [entry['url'] for entry in payload['data']])

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


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalNewsReportsAPIViewTests(ReportRequestFixture, TestCase):

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

    def test_hotfix_archive_filters_all_history_before_pagination(self):
        now = timezone.now()
        older = WowHotfixReport.objects.create(
            branch='wow', locale='enUS', build_num='69933',
            from_push=112181, to_push=112208, summary_title='圣骑士 热修',
            changed_tables_json='["SpellEffect", "SpellMisc"]', table_count=2, entry_count=15,
        )
        self._set_created_at(WowHotfixReport, older, now - timedelta(days=120))
        another = WowHotfixReport.objects.create(
            branch='wowt', locale='enUS', build_num='69933',
            from_push=112182, to_push=112209, summary_title='PTR 热修',
        )
        self._set_created_at(WowHotfixReport, another, now - timedelta(days=100))
        response = PortalHotfixReportsAPIView.as_view()(
            self.factory.get('/portal/api/hotfix-reports/?scope=all&branch=wow&q=112208&page_size=1')
        )
        payload = json.loads(response.content)
        self.assertEqual(payload['meta']['total'], 1)
        self.assertEqual([item['id'] for item in payload['data']], [older.id])
        self.assertEqual(payload['data'][0]['entry_count'], 15)
        for query in ('69933', 'SpellEffect', '圣骑士'):
            response = PortalHotfixReportsAPIView.as_view()(
                self.factory.get('/portal/api/hotfix-reports/', {'scope': 'all', 'branch': 'wow', 'q': query})
            )
            self.assertEqual([item['id'] for item in json.loads(response.content)['data']], [older.id])

    def test_news_page_defaults_to_news_and_declares_lazy_report_tabs(self):
        response = self.client.get('/portal/news/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('data-news-tab="news"', html)
        self.assertIn('data-news-tab="build"', html)
        self.assertIn('data-news-tab="hotfix"', html)
        self.assertIn('aria-selected="true"', html)
        self.assertIn('新闻资讯', html)

    def test_news_hotfix_button_and_legacy_deep_link_use_continuous_table(self):
        path = Path(__file__).resolve().parents[2] / 'static/portal/js/news_index.js'
        script = path.read_text(encoding='utf-8')
        self.assertIn("window.location.assign('/portal/wow-updates/?tab=hotfix')", script)
        self.assertIn("window.location.replace('/portal/wow-updates/?tab=hotfix')", script)
        updates = (Path(__file__).resolve().parents[2] /
                   'static/portal/js/wow-updates.js').read_text(encoding='utf-8')
        self.assertIn("new URLSearchParams(window.location.search).get('tab')", updates)
        self.assertIn("activateTab('hotfix')", updates)

    def test_archive_distinguishes_verified_source_facts_from_legacy_report(self):
        legacy = WowHotfixReport.objects.create(
            branch='wow', locale='enUS', from_push=112181, to_push=112208,
        )
        verified = WowHotfixReport.objects.create(
            branch='wow', locale='enUS', region_id=3, from_push=112181, to_push=112208,
            collection_complete=True, source_facts_json='[]',
        )
        response = PortalHotfixReportsAPIView.as_view()(
            self.factory.get('/portal/api/hotfix-reports/?scope=all&page_size=20')
        )
        flags = {row['id']: row['verified'] for row in json.loads(response.content)['data']}
        self.assertEqual(flags, {legacy.id: False, verified.id: True})
