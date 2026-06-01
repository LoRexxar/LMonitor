import json
import re
import time

import requests
import urllib3
from django.conf import settings as django_settings

from botend.controller.BaseScan import BaseScan
from botend.models import PortalMplusRun
from utils.log import logger


def _get(obj, path, default=None):
    cur = obj
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


class PortalMplusRunMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        try:
            base = (url or "").strip()
            season = "season-mn-1"
            region = "world"
            dungeons = self._get_season_dungeons(season) or []
            if not dungeons:
                api = base or f"https://raider.io/api/v1/mythic-plus/runs?season={season}&region={region}&dungeon=all&page=0"
                payload = self._fetch_json(api)
                rankings = (payload.get("rankings") or []) if payload else []
                for row in rankings:
                    self._upsert_row(row, source="raiderio", season=season, region=region)
                return True

            for d in dungeons:
                slug = (d.get("slug") or "").strip()
                if not slug:
                    continue
                api = base or f"https://raider.io/api/v1/mythic-plus/runs?season={season}&region={region}&dungeon={slug}&page=0"
                payload = self._fetch_json(api)
                rankings = (payload.get("rankings") or []) if payload else []
                for row in rankings:
                    self._upsert_row(row, source="raiderio", season=season, region=region)
            return True
        except Exception as e:
            logger.error(f"[PortalMplusRunMonitor] error: {str(e)}")
            return False

    def _get_proxies(self):
        proxies = getattr(django_settings, 'PROXY_CONFIG', None)
        return proxies if isinstance(proxies, dict) and proxies else None

    def _request_with_retry(self, url, timeout=25, retries=3, parse_json=True):
        proxies = self._get_proxies()
        headers = {"User-Agent": "Mozilla/5.0"}
        last_err = None
        for attempt in range(retries):
            try:
                if attempt > 0:
                    time.sleep(1 + attempt)
                resp = requests.get(url, timeout=timeout, headers=headers, proxies=proxies)
                if resp.status_code != 200:
                    return None
                if parse_json:
                    return resp.json() or {}
                return resp.text or ''
            except (requests.exceptions.SSLError, urllib3.exceptions.SSLError) as e:
                last_err = e
                logger.warning(f"[PortalMplusRunMonitor] SSL error (attempt {attempt + 1}/{retries}): {e}")
                continue
            except requests.exceptions.RequestException as e:
                last_err = e
                logger.warning(f"[PortalMplusRunMonitor] fetch failed (attempt {attempt + 1}/{retries}): {e}")
                continue
        logger.warning(f"[PortalMplusRunMonitor] fetch failed after {retries} retries: {last_err}")
        return None

    def _fetch_json(self, url):
        try:
            resp = self.req.getResponse(url, '', headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            if resp is not None and getattr(resp, 'status_code', 0) == 200:
                return resp.json() or {}
        except Exception:
            pass
        result = self._request_with_retry(url, timeout=25, retries=3, parse_json=True)
        return result

    def _get_season_dungeons(self, season_slug):
        try:
            url = "https://raider.io/api/v1/mythic-plus/static-data?expansion_id=11"
            try:
                resp = self.req.getResponse(url, '', headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                if resp is not None and getattr(resp, 'status_code', 0) == 200:
                    payload = resp.json() or {}
                    seasons = payload.get("seasons") or []
                    for s in seasons:
                        if (s.get("slug") or "").strip() == season_slug:
                            return s.get("dungeons") or []
            except Exception:
                pass
            payload = self._request_with_retry(url, timeout=25, retries=3, parse_json=True)
            if not payload:
                return []
            seasons = payload.get("seasons") or []
            for s in seasons:
                if (s.get("slug") or "").strip() == season_slug:
                    return s.get("dungeons") or []
        except Exception:
            pass
        return []

    def _upsert_row(self, row, source, season=None, region=None):
        rank = row.get("rank") or 0
        score = row.get("score")
        run = row.get("run") or {}
        run_id = run.get("keystone_run_id") or run.get("id")
        run_url = f"https://raider.io/mythic-plus-runs/{run_id}" if run_id else None

        dungeon = _get(run, ["dungeon", "name"]) or _get(run, ["dungeon", "short_name"]) or run.get("dungeon") or ""
        dungeon_slug = _get(run, ["dungeon", "slug"]) or None
        level = run.get("mythic_level") or run.get("keystone_level") or run.get("level") or 0
        clear_ms = run.get("clear_time_ms") or run.get("duration_ms") or run.get("duration") or 0
        try:
            time_seconds = int(int(clear_ms) / 1000) if int(clear_ms) > 10000 else int(clear_ms)
        except Exception:
            time_seconds = 0

        members = _get(run, ["roster", "members"], []) or _get(run, ["roster"], []) or []
        party = []
        tank = None
        healer = None
        dps = []
        for m in members:
            role = (m.get("role") or "").strip().lower()
            char = m.get("character") or {}
            name = (char.get("name") or "").strip()
            if not name:
                continue
            cls = char.get("class") or {}
            spec = char.get("spec") or {}
            party.append(
                {
                    "name": name,
                    "role": role or "",
                    "class": (cls.get("name") or "").strip(),
                    "class_slug": (cls.get("slug") or "").strip(),
                    "spec": (spec.get("name") or "").strip(),
                    "spec_slug": (spec.get("slug") or "").strip(),
                }
            )
            if role == "tank":
                tank = name
            elif role == "healer":
                healer = name
            else:
                dps.append(name)

        dps_json = None
        if dps:
            try:
                dps_json = json.dumps(dps, ensure_ascii=False)
            except Exception:
                dps_json = None

        party_json = None
        if party:
            try:
                party_json = json.dumps(party, ensure_ascii=False)
            except Exception:
                party_json = None

        PortalMplusRun.objects.update_or_create(
            source=source,
            season=season,
            region=region,
            rank=int(rank) if str(rank).isdigit() else 0,
            dungeon_slug=dungeon_slug,
            defaults={
                "dungeon": str(dungeon),
                "level": int(level) if str(level).isdigit() else 0,
                "time_seconds": time_seconds,
                "score": score,
                "run_url": run_url,
                "party_json": party_json,
                "tank": tank,
                "healer": healer,
                "dps_json": dps_json,
                "is_active": True,
            },
        )
