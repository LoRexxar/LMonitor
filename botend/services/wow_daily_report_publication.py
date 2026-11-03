"""Read the latest edition published during one Shanghai calendar day."""
from django.utils import timezone
from botend.models import WowDailyReport
from botend.wow_daily_report.site_sections import REPORT_ZONE, day_window


def current_daily_report(publication_date=None):
    day = publication_date or timezone.localdate(timezone=REPORT_ZONE)
    start, end = day_window(day)
    return (WowDailyReport.objects.filter(updated_at__gte=start, updated_at__lt=end,
            report_date__lte=day).order_by('-report_date', '-updated_at', '-id').first())
