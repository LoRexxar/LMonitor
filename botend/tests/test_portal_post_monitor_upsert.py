from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor, _hash_url


class PortalPostMonitorUpsertTests(SimpleTestCase):
    def test_upsert_uses_indexed_url_hash_for_lookup_and_locking(self):
        monitor = PortalPostMonitor.__new__(PortalPostMonitor)
        url = "https://wow.blizzard.cn/news/20260716/40565_1308032.html"
        url_hash = _hash_url(url)
        existing = MagicMock(publish_time=None)
        filtered = MagicMock()
        filtered.only.return_value.first.return_value = existing
        saved = MagicMock()

        with patch(
            "botend.controller.plugins.portal.PortalPostMonitor.WowArticle.objects"
        ) as objects:
            objects.filter.return_value = filtered
            objects.update_or_create.return_value = (saved, False)

            result = monitor._upsert_article(
                title="测试新闻",
                url=url,
                source="blizzard_cn",
                category="news",
            )

        self.assertIs(result, saved)
        objects.filter.assert_called_once_with(url_hash=url_hash)
        objects.update_or_create.assert_called_once()
        lookup_kwargs = objects.update_or_create.call_args.kwargs
        self.assertEqual(lookup_kwargs["url_hash"], url_hash)
        self.assertEqual(lookup_kwargs["defaults"]["url"], url)
