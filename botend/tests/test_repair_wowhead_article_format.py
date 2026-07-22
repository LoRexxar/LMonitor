from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.management.commands.repair_wowhead_article_format import Command


class RepairWowheadArticleFormatCommandTests(SimpleTestCase):
    @patch("botend.management.commands.repair_wowhead_article_format.MonitorTask.objects")
    @patch("botend.controller.plugins.wow.wowheadMonitor.wowheadMonitor")
    @patch("utils.LReq.LReq")
    def test_fetcher_inherits_active_wowhead_monitor_task(self, lreq_cls, monitor_cls, task_objects):
        task = Mock(proxy_enabled=True)
        task_objects.filter.return_value.first.return_value = task
        req = lreq_cls.return_value
        fetcher = Mock()
        monitor_cls.return_value = fetcher

        result = Command()._build_wowhead_fetcher()

        task_objects.filter.assert_called_once_with(name="wowheadMonitor", is_active=True)
        req.set_current_task.assert_called_once_with(task)
        monitor_cls.assert_called_once_with(req, task)
        self.assertIs(result, fetcher)
