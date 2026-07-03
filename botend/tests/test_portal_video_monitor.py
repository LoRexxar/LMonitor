import json

from django.test import TestCase

from botend.controller.plugins.portal.PortalVideoMonitor import PortalVideoMonitor
from botend.models import PortalVideo, VideoMonitorTarget


class _Response:
    def __init__(self, payload, status_code=200):
        self.text = json.dumps(payload)
        self.status_code = status_code


class _Request:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls = []

    def get(self, url, *args):
        self.urls.append(url)
        for key, payload in self.payloads:
            if key in url:
                return _Response(payload)
        return _Response({"code": -404, "message": "missing mock"})


class PortalVideoMonitorTest(TestCase):
    def _target(self, last_seen_bvid=None):
        return VideoMonitorTarget.objects.create(
            name="劳瑞就是LoRexxar",
            tag="wow",
            platform="bilibili",
            target_url="https://space.bilibili.com/20325887/video",
            last_seen_bvid=last_seen_bvid,
        )

    def test_dynamic_feed_creates_video_with_full_fields(self):
        target = self._target()
        payload = {
            "code": 0,
            "message": "0",
            "data": {
                "items": [
                    {
                        "type": "DYNAMIC_TYPE_DRAW",
                        "modules": {},
                    },
                    {
                        "type": "DYNAMIC_TYPE_AV",
                        "modules": {
                            "module_author": {"name": "劳瑞", "pub_ts": 1782323477},
                            "module_dynamic": {
                                "major": {
                                    "type": "MAJOR_TYPE_ARCHIVE",
                                    "archive": {
                                        "bvid": "BV1dQ7x65E7z",
                                        "title": "第九期下",
                                        "jump_url": "//www.bilibili.com/video/BV1dQ7x65E7z",
                                        "cover": "https://i0.hdslb.com/cover.jpg",
                                    },
                                }
                            },
                        },
                    },
                ]
            },
        }

        monitor = PortalVideoMonitor(_Request([("web-dynamic", payload)]), None)
        monitor.update_target(target)

        video = PortalVideo.objects.get(bvid="BV1dQ7x65E7z")
        self.assertEqual(video.title, "第九期下")
        self.assertEqual(video.url, "https://www.bilibili.com/video/BV1dQ7x65E7z")
        self.assertEqual(video.author_name, "劳瑞")
        self.assertEqual(video.author_url, target.target_url)
        self.assertEqual(video.cover_url, "https://i0.hdslb.com/cover.jpg")
        self.assertEqual(video.tag, "wow")
        self.assertEqual(video.target, target)
        self.assertIsNotNone(video.published_at)
        target.refresh_from_db()
        self.assertEqual(target.last_seen_bvid, "BV1dQ7x65E7z")

    def test_dynamic_rate_limit_does_not_fallback_to_arc(self):
        target = self._target()
        dynamic_payload = {"code": -799, "message": "请求过于频繁"}
        arc_payload = {
            "code": 0,
            "message": "0",
            "data": {
                "list": {
                    "vlist": [
                        {
                            "bvid": "BV1fallback",
                            "title": "投稿接口兜底",
                            "pic": "https://i0.hdslb.com/fallback.jpg",
                            "author": "劳瑞兜底",
                            "created": 1782323000,
                        }
                    ]
                }
            },
        }

        req = _Request([
            ("web-dynamic", dynamic_payload),
            ("arc/search", arc_payload),
        ])
        monitor = PortalVideoMonitor(req, None)
        monitor.update_target(target)

        self.assertFalse(PortalVideo.objects.filter(bvid="BV1fallback").exists())
        self.assertTrue(any("web-dynamic" in url for url in req.urls))
        self.assertFalse(any("arc/search" in url for url in req.urls))
        target.refresh_from_db()
        self.assertIsNone(target.last_seen_bvid)

    def test_arc_search_fallback_when_dynamic_succeeds_without_videos(self):
        target = self._target()
        dynamic_payload = {"code": 0, "message": "0", "data": {"items": []}}
        arc_payload = {
            "code": 0,
            "message": "0",
            "data": {
                "list": {
                    "vlist": [
                        {
                            "bvid": "BV1fallback",
                            "title": "投稿接口兜底",
                            "pic": "https://i0.hdslb.com/fallback.jpg",
                            "author": "劳瑞兜底",
                            "created": 1782323000,
                        }
                    ]
                }
            },
        }

        req = _Request([
            ("web-dynamic", dynamic_payload),
            ("arc/search", arc_payload),
        ])
        monitor = PortalVideoMonitor(req, None)
        monitor.update_target(target)

        video = PortalVideo.objects.get(bvid="BV1fallback")
        self.assertEqual(video.title, "投稿接口兜底")
        self.assertEqual(video.url, "https://www.bilibili.com/video/BV1fallback")
        self.assertEqual(video.author_name, "劳瑞兜底")
        self.assertEqual(video.cover_url, "https://i0.hdslb.com/fallback.jpg")
        self.assertTrue(any("web-dynamic" in url for url in req.urls))
        self.assertTrue(any("arc/search" in url for url in req.urls))
        target.refresh_from_db()
        self.assertEqual(target.last_seen_bvid, "BV1fallback")

    def test_stops_at_last_seen_bvid(self):
        target = self._target(last_seen_bvid="BV1old")
        payload = {
            "code": 0,
            "message": "0",
            "data": {
                "items": [
                    {
                        "type": "DYNAMIC_TYPE_AV",
                        "modules": {
                            "module_author": {"name": "劳瑞", "pub_ts": 1782323477},
                            "module_dynamic": {
                                "major": {
                                    "type": "MAJOR_TYPE_ARCHIVE",
                                    "archive": {
                                        "bvid": "BV1new",
                                        "title": "新视频",
                                        "jump_url": "//www.bilibili.com/video/BV1new",
                                        "cover": "https://i0.hdslb.com/new.jpg",
                                    },
                                }
                            },
                        },
                    },
                    {
                        "type": "DYNAMIC_TYPE_AV",
                        "modules": {
                            "module_author": {"name": "劳瑞", "pub_ts": 1782323000},
                            "module_dynamic": {
                                "major": {
                                    "type": "MAJOR_TYPE_ARCHIVE",
                                    "archive": {
                                        "bvid": "BV1old",
                                        "title": "旧视频",
                                        "jump_url": "//www.bilibili.com/video/BV1old",
                                    },
                                }
                            },
                        },
                    },
                    {
                        "type": "DYNAMIC_TYPE_AV",
                        "modules": {
                            "module_author": {"name": "劳瑞", "pub_ts": 1782322000},
                            "module_dynamic": {
                                "major": {
                                    "type": "MAJOR_TYPE_ARCHIVE",
                                    "archive": {
                                        "bvid": "BV1older",
                                        "title": "更旧视频",
                                        "jump_url": "//www.bilibili.com/video/BV1older",
                                    },
                                }
                            },
                        },
                    },
                ]
            },
        }

        monitor = PortalVideoMonitor(_Request([("web-dynamic", payload)]), None)
        monitor.update_target(target)

        self.assertTrue(PortalVideo.objects.filter(bvid="BV1new").exists())
        self.assertFalse(PortalVideo.objects.filter(bvid="BV1old").exists())
        self.assertFalse(PortalVideo.objects.filter(bvid="BV1older").exists())
        target.refresh_from_db()
        self.assertEqual(target.last_seen_bvid, "BV1new")
