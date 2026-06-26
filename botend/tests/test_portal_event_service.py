from datetime import datetime, timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from botend.services.portal_event_service import PortalEventService


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
