from botend.controller.BaseScan import BaseScan
from botend.models import PortalMythicstatsDpsRow
from botend.portal.mythicstats import fetch_current_season_slug, fetch_mythicstats_dps, upsert_mythicstats_dps_rows, upsert_mythicstats_meta_cache
from utils.log import logger


class PortalMythicstatsDpsMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        _season_hint = (url or "").strip() or (getattr(self.task, "target", "") or "").strip()
        if _season_hint in {"season-mn-1", "auto"}:
            _season_hint = ""

        season = _season_hint
        if not season:
            slug, _label = fetch_current_season_slug(req=self.req)
            if slug:
                season = slug

        payload = fetch_mythicstats_dps(req=self.req, season=season, dungeon_id=0, period_id=None)
        season = payload.get("season") or season or "unknown"
        periods = payload.get("periods") or []
        dungeons = payload.get("dungeons") or [{"id": 0, "name": "All dungeons"}]
        upsert_mythicstats_meta_cache(season=season, dungeons=dungeons, periods=periods)

        if not periods:
            logger.error("[PortalMythicstatsDpsMonitor] no periods found")
            return False

        top3 = periods[:3]
        latest = top3[0]
        for idx, p in enumerate(top3):
            pid = p.get("id")
            if not pid:
                continue
            cur = payload
            if int(pid) != int(payload.get("period_id") or 0):
                cur = fetch_mythicstats_dps(req=self.req, season=season, dungeon_id=0, period_id=int(pid))
            cur_season = season
            exists = PortalMythicstatsDpsRow.objects.filter(season=cur_season, period_id=int(pid), dungeon_id=0).exists()
            if idx > 0 and exists:
                continue
            period_label = cur.get("period_label") or str(pid)
            rankings = cur.get("rankings") or {}

            for role in ("damage", "tank", "healer"):
                rows = rankings.get(role) or []
                upsert_mythicstats_dps_rows(
                    season=cur_season,
                    period_id=int(pid),
                    period_label=period_label,
                    dungeon_id=0,
                    dungeon_name="All dungeons",
                    role=role,
                    rows=rows,
                    replace_batch=(idx == 0),
                )

        latest_period_id = int(latest.get("id") or 0)
        if latest_period_id:
            self.task.flag = f"{season}@{latest_period_id}"
            self.task.save()
        return True
