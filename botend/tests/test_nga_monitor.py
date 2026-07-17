from unittest.mock import Mock, patch

from django.test import TestCase

from botend.controller.plugins.wow.ngaMonitor import ngaMonitor
from botend.models import TargetAuth, WowArticle


NGA_GB18030_HTML = """
<html><head><meta charset="GB18030"></head><body>
<div id="topicrows"><tbody><tr>
<td>21</td><td><a class="topic" href="/read.php?tid=123">前瞻测试标题</a></td>
<td><span class="silver postdate">2026-07-14</span></td>
</tr></tbody></div>
</body></html>
""".encode("gb18030")


class NgaMonitorTests(TestCase):
    @patch('botend.controller.plugins.wow.ngaMonitor.upsert_system_alert')
    @patch('botend.controller.plugins.wow.ngaMonitor.TargetAuth.objects.filter')
    def test_scan_uses_bbs_domain_only(self, auth_filter, alert):
        auth_filter.return_value.first.return_value = None
        req = Mock()
        success = Mock(status_code=200, content=NGA_GB18030_HTML)
        req.get.side_effect = [success, success]
        monitor = ngaMonitor(req, Mock(flag=''))
        monitor.resolve_data = Mock()

        self.assertTrue(monitor.scan(''))

        self.assertEqual(req.get.call_count, 2)
        for call in req.get.call_args_list:
            self.assertIn('bbs.nga.cn', call.args[0])
            self.assertNotIn('nga.178.com', call.args[0])
        self.assertEqual(monitor.resolve_data.call_count, 2)
        monitor.resolve_data.assert_any_call(NGA_GB18030_HTML, '前瞻区', 10)
        alert.assert_not_called()

    @patch('botend.controller.plugins.wow.ngaMonitor.upsert_system_alert')
    @patch('botend.controller.plugins.wow.ngaMonitor.TargetAuth.objects.filter')
    def test_scan_returns_false_and_reports_cookie_alert_when_bbs_domain_forbidden(self, auth_filter, alert):
        auth_filter.return_value.first.return_value = None
        req = Mock()
        req.get.return_value = Mock(status_code=403, content=b'forbidden')
        monitor = ngaMonitor(req, Mock(flag=''))
        monitor.target_list = {'前瞻区': monitor.target_list['前瞻区']}

        self.assertFalse(monitor.scan(''))
        self.assertEqual(req.get.call_count, 1)
        alert.assert_called_once_with(
            category='NGA_COOKIE_REQUIRED',
            subject='前瞻区',
            level=3,
            title='NGA 前瞻区抓取失败',
            content='认证失败（HTTP 403），请更新 TargetAuth 的 NGA 登录 Cookie',
        )

    @patch('botend.controller.plugins.wow.ngaMonitor.upsert_system_alert')
    @patch('botend.controller.plugins.wow.ngaMonitor.TargetAuth.objects.filter')
    def test_scan_reports_response_change_for_200_without_topicrows(self, auth_filter, alert):
        auth_filter.return_value.first.return_value = None
        req = Mock()
        req.get.return_value = Mock(status_code=200, content=b'<html>challenge</html>')
        monitor = ngaMonitor(req, Mock(flag=''))
        monitor.target_list = {'前瞻区': monitor.target_list['前瞻区']}

        self.assertFalse(monitor.scan(''))
        alert.assert_called_once()
        self.assertEqual(alert.call_args.kwargs['category'], 'NGA_RESPONSE_CHANGED')

    def test_scan_uses_saved_nga_cookie_for_forum_requests(self):
        TargetAuth.objects.create(
            domain="bbs.nga.cn",
            cookie="ngaPassportUid=123; ngaPassportCid=token",
            is_login=True,
        )
        req = Mock()
        req.get.return_value = Mock(status_code=200, content=b'<div id="topicrows"></div>')
        task = Mock()
        monitor = ngaMonitor(req, task)

        monitor.scan("")

        self.assertEqual(req.get.call_count, 2)
        for call in req.get.call_args_list:
            self.assertEqual(call.args[1:3], ("Response", 0))
            self.assertEqual(call.args[3], "ngaPassportUid=123; ngaPassportCid=token")

    def test_normalize_nga_url_rewrites_legacy_absolute_url(self):
        monitor = ngaMonitor(Mock(), Mock())

        self.assertEqual(
            monitor.normalize_nga_url('https://nga.178.com/read.php?tid=123'),
            'https://bbs.nga.cn/read.php?tid=123',
        )

    def test_resolve_data_decodes_nga_gb18030_html(self):
        task = Mock()
        monitor = ngaMonitor(Mock(), task)
        monitor.trigger_webhook = Mock()

        monitor.resolve_data(NGA_GB18030_HTML, "前瞻区", 10)

        article = WowArticle.objects.get(url="https://bbs.nga.cn/read.php?tid=123")
        self.assertEqual(article.title, "前瞻测试标题")
        self.assertEqual(article.author, "nga前瞻区")

    def test_resolve_data_marks_existing_hot_article_as_nga_preview(self):
        article = WowArticle.objects.create(
            title="旧标题",
            url="https://bbs.nga.cn/read.php?tid=123",
            source="nga",
            category="hot",
            author=None,
            reply_count=1,
        )
        task = Mock()
        monitor = ngaMonitor(Mock(), task)

        monitor.resolve_data(NGA_GB18030_HTML, "前瞻区", 10)

        article.refresh_from_db()
        self.assertEqual(article.author, "nga前瞻区")
        self.assertEqual(article.category, "nga")
        self.assertEqual(article.reply_count, 21)

    def test_water_scan_does_not_overwrite_preview_classification(self):
        article = WowArticle.objects.create(
            title="前瞻测试标题",
            url="https://bbs.nga.cn/read.php?tid=123",
            source="nga",
            category="nga",
            author="nga前瞻区",
            reply_count=20,
        )
        monitor = ngaMonitor(Mock(), Mock())

        monitor.resolve_data(NGA_GB18030_HTML, "水区", 200)

        article.refresh_from_db()
        self.assertEqual(article.author, "nga前瞻区")
        self.assertEqual(article.reply_count, 21)
