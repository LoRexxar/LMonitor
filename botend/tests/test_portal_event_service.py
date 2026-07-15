from datetime import datetime, timedelta, timezone as dt_timezone

from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone

from botend.models import PortalEvent
from botend.services.portal_event_service import PortalEventService, decode_response_utf8, repair_utf8_mojibake


def _db2_calendar_time(year, month, day, hour=8, minute=0):
    return (
        ((year - 2000) << 24)
        | ((month - 1) << 20)
        | ((day - 1) << 14)
        | (hour << 6)
        | minute
    )


class PortalEventServiceDb2Test(SimpleTestCase):
    def _name_rows(self):
        return [
            {"ID": "10", "Name_lang": "战场假日活动"},
            {"ID": "20", "Name_lang": "仲夏火焰节"},
        ]

    def test_decode_response_utf8_prefers_bytes_over_wrong_text_encoding(self):
        class FakeResponse:
            content = "ID,Name_lang\n1,暗月马戏团\n".encode("utf-8")
            text = content.decode("latin-1")
            encoding = "ISO-8859-1"
            apparent_encoding = "ISO-8859-1"

        self.assertIn("暗月马戏团", decode_response_utf8(FakeResponse()))

    def test_repair_utf8_mojibake_handles_cp1252_and_nbsp_space(self):
        self.assertEqual(repair_utf8_mojibake("å® ç‰©å¯¹æˆ˜å\x81‡æ—¥æ´»åŠ¨"), "宠物对战假日活动")
        self.assertEqual(repair_utf8_mojibake("æš—æœˆé©¬æˆ\x8få›¢"), "暗月马戏团")

    def test_parse_db2_holidays_repairs_mojibake_name_and_description(self):
        rows = [{
            "ID": "100",
            "Region": "2",
            "HolidayNameID": "10",
            "HolidayDescriptionID": "30",
            "Date_0": str(_db2_calendar_time(2026, 6, 25)),
            "Duration_0": "168",
        }]
        name_rows = [{"ID": "10", "Name_lang": "å® ç‰©å¯¹æˆ˜å\x81‡æ—¥æ´»åŠ¨"}]
        description_rows = [{"ID": "30", "Description_lang": "åœ¨æ\xad¤æ´»åŠ¨æœŸé—´ï¼Œä½ çš„å® ç‰©å\x8f¯èŽ·å¾—å\x8fŒå€\x8dç»\x8féªŒå€¼ï¼\x81"}]

        events = PortalEventService().parse_db2_holidays(rows, name_rows, description_rows, build="test-build")

        self.assertEqual(events[0].title, "宠物对战假日活动")
        self.assertEqual(events[0].summary, "在此活动期间，你的宠物可获得双倍经验值！")

    def test_parse_db2_holidays_keeps_cn_and_global_regions_only(self):
        rows = [
            {
                "ID": "100",
                "Region": "2",
                "HolidayNameID": "10",
                "HolidayDescriptionID": "",
                "Date_0": str(_db2_calendar_time(2026, 6, 25)),
                "Duration_0": "168",
            },
            {
                "ID": "200",
                "Region": "0",
                "HolidayNameID": "20",
                "HolidayDescriptionID": "",
                "Date_0": str(_db2_calendar_time(2026, 6, 21)),
                "Duration_0": "336",
            },
            {
                "ID": "300",
                "Region": "1",
                "HolidayNameID": "10",
                "HolidayDescriptionID": "",
                "Date_0": str(_db2_calendar_time(2026, 6, 23)),
                "Duration_0": "168",
            },
        ]

        events = PortalEventService().parse_db2_holidays(rows, self._name_rows(), [], build="test-build")

        self.assertCountEqual([event.title for event in events], ["仲夏火焰节", "战场假日活动"])
        self.assertEqual({event.raw_data["region"] for event in events}, {"0", "2"})
        self.assertEqual({event.raw_data["region_scope"] for event in events}, {"global", "cn"})

    def test_parse_db2_holidays_expands_looping_occurrences(self):
        service = PortalEventService()
        row = {
            "ID": "100",
            "Region": "2",
            "HolidayNameID": "10",
            "HolidayDescriptionID": "",
            "Date_0": str(_db2_calendar_time(2026, 6, 25)),
            "Duration_0": "168",
            "Duration_1": "1680",
            "Looping": "1",
        }
        base_start = timezone.make_aware(datetime(2026, 6, 25, 8), timezone.get_current_timezone())

        events = service.parse_db2_holidays([row], self._name_rows(), [], build="test-build")
        starts = [event.start_at for event in events]

        self.assertIn(base_start, starts)
        self.assertIn(base_start + timedelta(hours=1680), starts)
        self.assertTrue(all(event.raw_data["is_looping"] for event in events))
        self.assertTrue(all(event.raw_data["loop_interval_hours"] == 1680 for event in events))
        self.assertTrue(all(event.end_at == event.start_at + timedelta(hours=168) for event in events))


class PortalEventServiceWowheadTest(SimpleTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch(
            "botend.services.portal_event_service.timezone.now",
            return_value=datetime(2026, 6, 15, 12, tzinfo=dt_timezone.utc),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_wowhead_events_shifts_to_cn_calendar_time(self):
        original_start = int(datetime(2026, 6, 16, 15, tzinfo=dt_timezone.utc).timestamp())
        original_end = int(datetime(2026, 6, 23, 15, tzinfo=dt_timezone.utc).timestamp())
        html = f'''
            <script>
            window.__DATA__ = {{"groups":[{{"content":{{"lines":[{{
                "name":"World Quest Bonus Event",
                "url":"/event=592/world-quest-bonus-event",
                "startingUt":"{original_start}",
                "endingUt":{original_end},
                "icon":"achievement_reputation_08"
            }}]}},"id":"holiday","name":"World Event"}}]}};
            </script>
        '''

        events = PortalEventService().parse_wowhead_events(html, source_url="https://www.wowhead.com/events")

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.title, "世界任务奖励活动")
        self.assertEqual(event.source, "wowhead_cn_derived")
        self.assertEqual(event.raw_data["time_shift_days"], 2)
        self.assertEqual(event.raw_data["cn_start_hour"], 8)
        self.assertEqual(event.start_at.date(), datetime(2026, 6, 18).date())
        self.assertEqual(event.start_at.hour, 8)
        self.assertEqual(event.end_at, event.start_at + timedelta(days=7))
        self.assertIn("/event=592/", event.url)

    def test_parse_wowhead_events_keeps_unknown_event_title(self):
        original_start = int(datetime(2026, 7, 5, 6, tzinfo=dt_timezone.utc).timestamp())
        html = f'''
            <script>
            {{"name":"Darkmoon Faire","url":"/guide/world-events/recurring/darkmoon-faire-guide","startingUt":"{original_start}"}}
            </script>
        '''

        events = PortalEventService().parse_wowhead_events(html)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "暗月马戏团")
        self.assertEqual(events[0].start_at.date(), datetime(2026, 7, 7).date())

    def test_parse_wowhead_events_expands_events_page_occurrences(self):
        html = '''
            <script>
            new Listview({template:'event',data:[{
                "id":592,
                "name":"World Quest Bonus Event",
                "url":"/event=592/world-quest-bonus-event",
                "occurrences":[{"start":"2026/06/16 08:00:00","end":"2026/06/23 07:00:00"}]
            }]});
            </script>
        '''

        events = PortalEventService().parse_wowhead_events(html, source_url="https://www.wowhead.com/events")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "世界任务奖励活动")
        self.assertEqual(events[0].start_at.date(), datetime(2026, 6, 18).date())
        self.assertEqual(events[0].start_at.hour, 8)
        self.assertEqual(events[0].end_at, events[0].start_at + timedelta(hours=167))


class PortalEventsApiTest(TestCase):
    def test_events_api_returns_events_overlapping_visible_window(self):
        now = timezone.make_aware(datetime(2026, 7, 1, 12), timezone.get_current_timezone())
        overlapping = PortalEvent.objects.create(
            title="跨月活动",
            url="https://example.com/overlap",
            url_hash="overlap-event",
            source="test",
            start_at=now - timedelta(days=20),
            end_at=now + timedelta(days=3),
            status="进行中",
            is_active=True,
        )
        PortalEvent.objects.create(
            title="已结束活动",
            url="https://example.com/ended",
            url_hash="ended-event",
            source="test",
            start_at=now - timedelta(days=20),
            end_at=now - timedelta(days=8),
            is_active=True,
        )
        PortalEvent.objects.create(
            title="未来活动",
            url="https://example.com/future",
            url_hash="future-event",
            source="test",
            start_at=now + timedelta(days=50),
            end_at=now + timedelta(days=57),
            is_active=True,
        )

        with patch("botend.portal.api.timezone.now", return_value=now):
            response = Client().get("/portal/api/events/")

        self.assertEqual(response.status_code, 200)
        titles = [item["title"] for item in response.json()["data"]]
        self.assertIn(overlapping.title, titles)
        self.assertNotIn("已结束活动", titles)
        self.assertNotIn("未来活动", titles)
