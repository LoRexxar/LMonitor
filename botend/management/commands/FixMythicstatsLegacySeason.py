from django.core.management.base import BaseCommand

from botend.models import PortalMythicstatsDpsRow
from botend.portal.mythicstats import fetch_period_season_slug


class Command(BaseCommand):
    def handle(self, *args, **options):
        period_ids = list(
            PortalMythicstatsDpsRow.objects.filter(season="season-mn-1")
            .values_list("period_id", flat=True)
            .distinct()
            .order_by("-period_id")
        )
        updated = 0
        for pid in period_ids:
            slug, _label = fetch_period_season_slug(req=None, period_id=int(pid))
            if not slug:
                continue
            qs = PortalMythicstatsDpsRow.objects.filter(season="season-mn-1", period_id=int(pid))
            cnt = qs.update(season=slug)
            updated += int(cnt or 0)
        self.stdout.write(f"updated_rows={updated}")

