import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.wow_daily_report.generator import generate_wow_daily_report


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--date", default="", help="YYYY-MM-DD")
        parser.add_argument("--use-llm", action="store_true")

    def handle(self, *args, **options):
        raw = (options.get("date") or "").strip()
        if raw:
            report_date = datetime.date.fromisoformat(raw)
        else:
            report_date = timezone.localdate()
        use_llm = bool(options.get("use_llm"))
        meta = generate_wow_daily_report(report_date=report_date, use_llm=use_llm)
        self.stdout.write(f"date={report_date.isoformat()}")
        self.stdout.write(f"md_path={meta.get('md_path')}")
        self.stdout.write(f"full_path={meta.get('full_path')}")

