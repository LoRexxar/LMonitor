import re
import time

import requests
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.controller.BaseScan import BaseScan
from botend.models import PortalPeakSpecRankRow
from utils.log import logger


class PortalPeakSpecRankMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        season = self._resolve_season()
        region = "world"
        ok = True

        for cls in self._spec_list():
            class_slug = cls.get("class_slug") or ""
            spec_slug = cls.get("spec_slug") or ""
            if not class_slug or not spec_slug:
                continue
            if not self._fetch_and_upsert(season=season, region=region, class_slug=class_slug, spec_slug=spec_slug):
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
            now = timezone.now()
            current = []
            started = []
            for season in seasons:
                slug = (season.get("slug") or "").strip()
                starts = parse_datetime(((season.get("starts") or {}).get("us") or "").strip())
                ends = parse_datetime(((season.get("ends") or {}).get("us") or "").strip())
                if starts and timezone.is_naive(starts):
                    starts = timezone.make_aware(starts, timezone.utc)
                if ends and timezone.is_naive(ends):
                    ends = timezone.make_aware(ends, timezone.utc)
                if not re.fullmatch(r'season-[a-z0-9]+-\d+', slug) or not starts or starts > now:
                    continue
                started.append((starts, slug))
                if not ends or now < ends:
                    current.append((starts, slug))
            candidates = current or started
            if candidates:
                return max(candidates)[1]
        except Exception:
            return "season-mn-1"
        return "season-mn-1"

    def _fetch_and_upsert(self, *, season, region, class_slug, spec_slug):
        api = "https://raider.io/api/mythic-plus/rankings/specs"
        def fetch_page(page):
            last_status = None
            last_payload = None
            for attempt in range(3):
                try:
                    params = {
                        "season": season,
                        "region": region,
                        "class": class_slug,
                        "spec": spec_slug,
                        "page": page,
                        "pageSize": 20,
                    }
                    resp = requests.get(api, params=params, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
                    last_status = resp.status_code
                    if resp.status_code != 200:
                        time.sleep(0.6 + attempt * 0.6)
                        continue
                    last_payload = resp.json() or {}
                    return last_payload, last_status
                except Exception as e:
                    logger.warning(
                        f"[PortalPeakSpecRankMonitor] fetch error: {class_slug}/{spec_slug} page={page} err={str(e)}"
                    )
                    time.sleep(0.6 + attempt * 0.6)
            return last_payload, last_status

        top_rows = []
        seen = set()
        page = 0
        last_status = None
        while len(top_rows) < 3 and page < 5:
            payload, last_status = fetch_page(page)
            if not payload:
                logger.warning(
                    f"[PortalPeakSpecRankMonitor] fetch failed: {class_slug}/{spec_slug} page={page} status={last_status}"
                )
                return False

            rankings = payload.get("rankings") or {}
            rows = rankings.get("rankedCharacters") or []
            if not isinstance(rows, list):
                rows = []

            if not rows:
                break

            for row in rows:
                if len(top_rows) >= 3:
                    break
                char = row.get("character") or {}
                char_path = (char.get("path") or "").strip()
                realm_obj = char.get("realm") or {}
                rio_region_obj = char.get("region") or {}
                realm_slug = (realm_obj.get("slug") or "").strip()
                rio_region_slug = (rio_region_obj.get("slug") or "").strip()
                char_name = (char.get("name") or "").strip()
                if not char_name:
                    continue

                dedupe_key = (char_path or f"{char_name}|{realm_slug}|{rio_region_slug}").lower()
                if not dedupe_key or dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                top_rows.append(row)

            page += 1
            time.sleep(0.2)

        if not top_rows:
            logger.warning(f"[PortalPeakSpecRankMonitor] empty rankings: {class_slug}/{spec_slug} season={season}")
            return False

        if len(top_rows) < 3:
            logger.warning(f"[PortalPeakSpecRankMonitor] not enough rows: {class_slug}/{spec_slug} rows={len(top_rows)}")
            return True

        PortalPeakSpecRankRow.objects.filter(
            season=season,
            region=region,
            class_slug=class_slug,
            spec_slug=spec_slug,
            is_active=True,
        ).update(is_active=False)

        for idx, row in enumerate(top_rows[:3]):
            rank = idx + 1

            score = row.get("score")
            score_color = (row.get("scoreColor") or "").strip()

            char = row.get("character") or {}
            char_name = (char.get("name") or "").strip()
            char_path = (char.get("path") or "").strip()

            class_obj = char.get("class") or {}
            spec_obj = char.get("spec") or {}
            realm_obj = char.get("realm") or {}
            rio_region_obj = char.get("region") or {}

            cur_class_name = (class_obj.get("name") or "").strip()
            cur_spec_name = (spec_obj.get("name") or "").strip()
            cur_spec_role = (spec_obj.get("role") or "").strip().lower()

            rio_region_slug = (rio_region_obj.get("slug") or "").strip()
            realm_slug = (realm_obj.get("slug") or "").strip()
            realm_name = (realm_obj.get("name") or "").strip()

            PortalPeakSpecRankRow.objects.update_or_create(
                season=season,
                region=region,
                class_slug=class_slug,
                spec_slug=spec_slug,
                rank=rank,
                defaults={
                    "class_name": cur_class_name,
                    "spec_name": cur_spec_name,
                    "spec_role": cur_spec_role,
                    "character_name": char_name,
                    "character_path": char_path,
                    "score": score,
                    "score_color": score_color,
                    "rio_region_slug": rio_region_slug,
                    "realm_slug": realm_slug,
                    "realm_name": realm_name,
                    "is_active": True,
                },
            )
        return True

    def _spec_list(self):
        return [
            {"class_slug": "death-knight", "spec_slug": "blood"},
            {"class_slug": "death-knight", "spec_slug": "frost"},
            {"class_slug": "death-knight", "spec_slug": "unholy"},
            {"class_slug": "demon-hunter", "spec_slug": "havoc"},
            {"class_slug": "demon-hunter", "spec_slug": "vengeance"},
            {"class_slug": "demon-hunter", "spec_slug": "devourer"},
            {"class_slug": "druid", "spec_slug": "balance"},
            {"class_slug": "druid", "spec_slug": "feral"},
            {"class_slug": "druid", "spec_slug": "guardian"},
            {"class_slug": "druid", "spec_slug": "restoration"},
            {"class_slug": "evoker", "spec_slug": "devastation"},
            {"class_slug": "evoker", "spec_slug": "preservation"},
            {"class_slug": "evoker", "spec_slug": "augmentation"},
            {"class_slug": "hunter", "spec_slug": "beast-mastery"},
            {"class_slug": "hunter", "spec_slug": "marksmanship"},
            {"class_slug": "hunter", "spec_slug": "survival"},
            {"class_slug": "mage", "spec_slug": "arcane"},
            {"class_slug": "mage", "spec_slug": "fire"},
            {"class_slug": "mage", "spec_slug": "frost"},
            {"class_slug": "monk", "spec_slug": "brewmaster"},
            {"class_slug": "monk", "spec_slug": "mistweaver"},
            {"class_slug": "monk", "spec_slug": "windwalker"},
            {"class_slug": "paladin", "spec_slug": "holy"},
            {"class_slug": "paladin", "spec_slug": "protection"},
            {"class_slug": "paladin", "spec_slug": "retribution"},
            {"class_slug": "priest", "spec_slug": "discipline"},
            {"class_slug": "priest", "spec_slug": "holy"},
            {"class_slug": "priest", "spec_slug": "shadow"},
            {"class_slug": "rogue", "spec_slug": "assassination"},
            {"class_slug": "rogue", "spec_slug": "outlaw"},
            {"class_slug": "rogue", "spec_slug": "subtlety"},
            {"class_slug": "shaman", "spec_slug": "elemental"},
            {"class_slug": "shaman", "spec_slug": "enhancement"},
            {"class_slug": "shaman", "spec_slug": "restoration"},
            {"class_slug": "warlock", "spec_slug": "affliction"},
            {"class_slug": "warlock", "spec_slug": "demonology"},
            {"class_slug": "warlock", "spec_slug": "destruction"},
            {"class_slug": "warrior", "spec_slug": "arms"},
            {"class_slug": "warrior", "spec_slug": "fury"},
            {"class_slug": "warrior", "spec_slug": "protection"},
        ]
