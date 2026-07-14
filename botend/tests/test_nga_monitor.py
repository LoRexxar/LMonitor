from unittest.mock import Mock

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


class NgaMonitorRequestTests(TestCase):
    def test_scan_uses_saved_nga_cookie_for_forum_requests(self):
        TargetAuth.objects.create(
            domain="nga.178.com",
            cookie="ngaPassportUid=123; ngaPassportCid=token",
            is_login=True,
        )
        req = Mock()
        req.get.return_value = b'<div id="topicrows"></div>'
        task = Mock()
        monitor = ngaMonitor(req, task)

        monitor.scan("")

        self.assertEqual(req.get.call_count, 2)
        for call in req.get.call_args_list:
            self.assertEqual(call.args[1:3], ("Resp", 0))
            self.assertEqual(call.args[3], "ngaPassportUid=123; ngaPassportCid=token")

    def test_resolve_data_decodes_nga_gb18030_html(self):
        task = Mock()
        monitor = ngaMonitor(Mock(), task)
        monitor.trigger_webhook = Mock()

        monitor.resolve_data(NGA_GB18030_HTML, "前瞻区", 10)

        article = WowArticle.objects.get(url="https://nga.178.com/read.php?tid=123")
        self.assertEqual(article.title, "前瞻测试标题")
        self.assertEqual(article.author, "nga前瞻区")
