"""Wednesday publication uses Tuesday's content day, independent of host time."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from botend.wow_daily_report.edition import (
    edition_for_report_date, published_edition, report_date_for,
)


class WeeklyEditionCalendarTests(SimpleTestCase):
    def test_tuesday_content_is_the_wednesday_special(self):
        self.assertEqual(edition_for_report_date(date(2026, 11, 10)), {
            'kind': 'wednesday_weekly', 'publication_date': '2026-11-11',
            'period_start': '2026-11-04', 'period_end': '2026-11-10',
            'timezone': 'Asia/Shanghai',
        })

    def test_other_six_content_days_are_daily(self):
        for offset in range(1, 7):
            day = date(2026, 11, 10) + timedelta(days=offset)
            with self.subTest(day=day):
                edition = edition_for_report_date(day)
                self.assertEqual(edition['kind'], 'daily')
                self.assertEqual(edition['period_start'], day.isoformat())
                self.assertEqual(edition['period_end'], day.isoformat())
                self.assertEqual(edition['publication_date'], (day+timedelta(days=1)).isoformat())

    def test_year_boundary_and_leap_day_are_complete_seven_days(self):
        for text, start, publish in [('2026-01-06', '2025-12-31', '2026-01-07'),
                                    ('2024-03-05', '2024-02-28', '2024-03-06')]:
            with self.subTest(text=text):
                edition = edition_for_report_date(text)
                self.assertEqual(edition['period_start'], start)
                self.assertEqual(edition['publication_date'], publish)
                self.assertEqual((date.fromisoformat(edition['period_end'])-date.fromisoformat(start)).days, 6)

    def test_monitor_content_day_uses_shanghai_not_activated_timezone(self):
        from django.utils import timezone
        instant = datetime.fromisoformat('2026-11-10T16:00:00+00:00')
        with timezone.override(ZoneInfo('America/New_York')):
            self.assertEqual(report_date_for(instant), date(2026, 11, 10))
            self.assertEqual(report_date_for(instant-timedelta(microseconds=1)), date(2026, 11, 9))
        with self.assertRaises(ValueError):
            report_date_for(datetime(2026, 11, 11))

    def test_old_tuesday_editions_without_explicit_metadata_stay_daily(self):
        for ext in [None, {}, {'sections': {'rio': {'count': 2}}}, {'edition': 'weekly'}]:
            self.assertIsNone(published_edition(date(2026, 11, 10), ext))

    def test_published_metadata_has_one_authoritative_date_contract(self):
        expected = edition_for_report_date(date(2026, 11, 10))
        self.assertEqual(published_edition(date(2026, 11, 10), {'edition': expected}), expected)
        for field, value in [('kind', 'daily'), ('period_start', '2026-11-05'),
                             ('publication_date', '2026-11-12'), ('timezone', 'UTC')]:
            bad = {**expected, field: value}
            self.assertIsNone(published_edition(date(2026, 11, 10), {'edition': bad}))
        self.assertIsNone(published_edition(date(2026, 11, 10), {'edition': {**expected, 'x': 1}}))

    def test_date_tokens_are_not_silently_repaired(self):
        for day in ['20261110', '2026-11-10T00:00:00', '2026-11-10 ', None, datetime(2026, 11, 10)]:
            with self.subTest(day=day), self.assertRaises((TypeError, ValueError)):
                edition_for_report_date(day)


class WeeklySummaryTests(SimpleTestCase):
    def test_fact_only_summary_keeps_signs_zero_and_missing_baselines(self):
        from botend.wow_daily_report.rio_weekly_presentation import _weekly_summary
        cases = [
            (10, -2, '本周参与角色净增加 10；净变化较上周减少 2。'),
            (-5, 2, '本周参与角色净减少 5；净变化较上周增加 2。'),
            (0, 0, '本周参与角色净变化为 0；净变化与上周持平。'),
            (None, None, '本周比较端点缺失，暂无法计算净变化；上周比较数据不完整。'),
            (10, None, '本周参与角色净增加 10；上周比较数据不完整。'),
        ]
        for net, diff, expected in cases:
            with self.subTest(net=net, diff=diff):
                self.assertEqual(_weekly_summary({'net_change': net, 'change_vs_previous': diff}), expected)
