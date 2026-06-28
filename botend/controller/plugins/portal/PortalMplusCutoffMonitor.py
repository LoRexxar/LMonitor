import time
from datetime import date, datetime, timezone

import requests

from botend.controller.BaseScan import BaseScan
from botend.models import PortalMplusSeasonCutoff
from utils.log import logger


def _get(obj, path, default=None):
    cur = obj
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


class PortalMplusCutoffMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        season = self._resolve_season()
        base = (url or "").strip()
        if base and "season-cutoffs" not in base:
            base = ""

        ok = True
        for region in ["us", "eu", "cn"]:
            if not self._fetch_and_upsert(season=season, region=region, base=base):
                ok = False
            time.sleep(0.2)

        if ok:
            try:
                self.task.flag = f"{season}@{int(time.time())}"
                self.task.save()
            except Exception:
                pass
        return ok

    def _resolve_season(self):
        try:
            resp = requests.get(
                "https://raider.io/api/v1/mythic-plus/static-data?expansion_id=11",
                timeout=25,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200:
                return "season-mn-1"
            payload = resp.json() or {}
            seasons = payload.get("seasons") or []
            now = datetime.now(timezone.utc)

            # Raider.IO may list a future season first before its cutoffs exist.
            # Pick the newest season that has already started, not blindly seasons[0].
            for season in seasons:
                slug = (season.get("slug") or "").strip()
                if not slug:
                    continue
                starts = season.get("starts") or {}
                start_values = [v for v in starts.values() if v]
                if not start_values:
                    return slug
                try:
                    earliest_start = min(
                        datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                        for v in start_values
                    )
                except Exception:
                    return slug
                if earliest_start <= now:
                    return slug
        except Exception:
            return "season-mn-1"
        return "season-mn-1"

    def _fetch_and_upsert(self, *, season, region, base=""):
        api = base or "https://raider.io/api/v1/mythic-plus/season-cutoffs"
        params = {"season": season, "region": region}
        last_status = None
        last_err = None
        payload = None
        for attempt in range(3):
            try:
                resp = requests.get(api, params=params, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
                last_status = resp.status_code
                if resp.status_code != 200:
                    time.sleep(0.6 + attempt * 0.6)
                    continue
                payload = resp.json() or {}
                break
            except Exception as e:
                last_err = str(e)
                time.sleep(0.6 + attempt * 0.6)

        if not payload:
            logger.warning(
                f"[PortalMplusCutoffMonitor] fetch failed: season={season} region={region} status={last_status} err={last_err}"
            )
            return False

        cutoff_0_1 = _get(payload, ["cutoffs", "p999", "all", "quantileMinValue"])
        cutoff_1 = _get(payload, ["cutoffs", "p990", "all", "quantileMinValue"])
        source_updated_at = _get(payload, ["cutoffs", "updatedAt"], "") or ""

        today = date.today()
        try:
            existing = PortalMplusSeasonCutoff.objects.filter(
                season=season, region=region
            ).first()
        except Exception:
            existing = None

        prev_0_1 = getattr(existing, 'cutoff_0_1_prev', None) if existing else None
        prev_1 = getattr(existing, 'cutoff_1_prev', None) if existing else None
        prev_date = getattr(existing, 'prev_updated_at', None) if existing else None

        if prev_date != today:
            prev_0_1 = getattr(existing, 'cutoff_0_1', None) if existing else None
            prev_1 = getattr(existing, 'cutoff_1', None) if existing else None
            prev_date = today

        PortalMplusSeasonCutoff.objects.update_or_create(
            season=season,
            region=region,
            defaults={
                "cutoff_0_1": cutoff_0_1,
                "cutoff_1": cutoff_1,
                "cutoff_0_1_prev": prev_0_1,
                "cutoff_1_prev": prev_1,
                "prev_updated_at": prev_date,
                "source": "raiderio",
                "source_updated_at": str(source_updated_at),
            },
        )
        return True

