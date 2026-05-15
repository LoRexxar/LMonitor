import time

import requests

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
            if seasons:
                slug = (seasons[0].get("slug") or "").strip()
                if slug:
                    return slug
        except Exception:
            return "season-mn-1"
        return "season-mn-1"

    def _fetch_and_upsert(self, *, season, region, class_slug, spec_slug):
        try:
            api = "https://raider.io/api/mythic-plus/rankings/specs"
            params = {
                "season": season,
                "region": region,
                "class": class_slug,
                "spec": spec_slug,
                "page": 0,
                "pageSize": 3,
            }
            resp = requests.get(api, params=params, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                logger.warning(f"[PortalPeakSpecRankMonitor] fetch failed: {class_slug}/{spec_slug} status={resp.status_code}")
                return False
            payload = resp.json() or {}
        except Exception as e:
            logger.warning(f"[PortalPeakSpecRankMonitor] fetch error: {class_slug}/{spec_slug} err={str(e)}")
            return False

        rankings = payload.get("rankings") or {}
        rows = rankings.get("rankedCharacters") or []
        if not isinstance(rows, list):
            rows = []

        if not rows:
            return True

        for row in rows[:3]:
            try:
                rank = int(row.get("rank") or 0)
            except Exception:
                rank = 0
            if rank <= 0:
                continue

            score = row.get("score")
            score_color = (row.get("scoreColor") or "").strip()

            char = row.get("character") or {}
            char_name = (char.get("name") or "").strip()
            char_path = (char.get("path") or "").strip()

            class_obj = char.get("class") or {}
            spec_obj = char.get("spec") or {}
            realm_obj = char.get("realm") or {}
            rio_region_obj = char.get("region") or {}

            cur_class_slug = (class_obj.get("slug") or class_slug).strip()
            cur_class_name = (class_obj.get("name") or "").strip()
            cur_spec_slug = (spec_obj.get("slug") or spec_slug).strip()
            cur_spec_name = (spec_obj.get("name") or "").strip()
            cur_spec_role = (spec_obj.get("role") or "").strip().lower()

            rio_region_slug = (rio_region_obj.get("slug") or "").strip()
            realm_slug = (realm_obj.get("slug") or "").strip()
            realm_name = (realm_obj.get("name") or "").strip()

            PortalPeakSpecRankRow.objects.update_or_create(
                season=season,
                region=region,
                class_slug=cur_class_slug,
                spec_slug=cur_spec_slug,
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

