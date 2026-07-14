from unittest.mock import Mock

from django.test import TestCase

from botend.controller.plugins.wow.ngaMonitor import ngaMonitor
from botend.models import TargetAuth


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
