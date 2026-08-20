import time

import requests
from django.db import transaction

from botend.controller.BaseScan import BaseScan
from botend.constants.wow import canonical_class_spec
from botend.models import PortalPeakSpecRankRow, SeasonMeta
from utils.log import logger


TOP_RANK_LIMIT = 20


class PortalPeakSpecRankMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        season = self._resolve_season()
        if not season:
            logger.error("[PortalPeakSpecRankMonitor] 活跃 SeasonMeta.rio_season 为空，跳过巅峰榜刷新")
            return False
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
        season = SeasonMeta.objects.filter(is_active=True).first()
        return (season.rio_season or "").strip() if season else ""

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
        while len(top_rows) < TOP_RANK_LIMIT and page < 5:
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
                if len(top_rows) >= TOP_RANK_LIMIT:
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

        if len(top_rows) < TOP_RANK_LIMIT:
            logger.warning(f"[PortalPeakSpecRankMonitor] not enough rows: {class_slug}/{spec_slug} rows={len(top_rows)}")
            return False

        top_rows = top_rows[:TOP_RANK_LIMIT]
        if not self._has_complete_unique_player_identities(top_rows):
            logger.warning(
                f"[PortalPeakSpecRankMonitor] invalid player identities: "
                f"{class_slug}/{spec_slug}"
            )
            return False

        self._persist_rank_snapshot(
            season=season,
            region=region,
            class_slug=class_slug,
            spec_slug=spec_slug,
            rows=top_rows,
        )

        canonical_identity = canonical_class_spec(class_slug, spec_slug)
        if canonical_identity:
            try:
                from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
                class_name, spec_name = canonical_identity
                SpecDetailPlayerMonitor(self.req, self.task).preload_peak_rankings(
                    rio_season=season,
                    class_name=class_name,
                    spec_name=spec_name,
                    rankings=top_rows,
                )
            except Exception as exc:
                # 人物预载是榜单写入后的附加动作；失败不能回滚已经刷新的榜单。
                logger.warning(
                    f"[PortalPeakSpecRankMonitor] player preload failed: "
                    f"{class_slug}/{spec_slug} err={exc}"
                )
        return True

    @staticmethod
    def _has_complete_unique_player_identities(rows):
        identities = set()
        for row in rows:
            character = (row or {}).get("character") or {}
            region = (character.get("region") or {}).get("slug") or ""
            realm = (character.get("realm") or {}).get("name") or ""
            name = character.get("name") or ""
            identity = tuple(str(value).strip().casefold() for value in (region, realm, name))
            if not all(identity) or identity in identities:
                return False
            identities.add(identity)
        return len(identities) == TOP_RANK_LIMIT

    def _persist_rank_snapshot(self, *, season, region, class_slug, spec_slug, rows):
        """将一个专精的有效 Top20 作为单个数据库快照写入。"""
        with transaction.atomic():
            PortalPeakSpecRankRow.objects.filter(
                season=season,
                region=region,
                class_slug=class_slug,
                spec_slug=spec_slug,
                is_active=True,
            ).update(is_active=False)

            for idx, row in enumerate(rows):
                rank = idx + 1
                char = row.get("character") or {}
                class_obj = char.get("class") or {}
                spec_obj = char.get("spec") or {}
                realm_obj = char.get("realm") or {}
                rio_region_obj = char.get("region") or {}

                PortalPeakSpecRankRow.objects.update_or_create(
                    season=season,
                    region=region,
                    class_slug=class_slug,
                    spec_slug=spec_slug,
                    rank=rank,
                    defaults={
                        "class_name": (class_obj.get("name") or "").strip(),
                        "spec_name": (spec_obj.get("name") or "").strip(),
                        "spec_role": (spec_obj.get("role") or "").strip().lower(),
                        "character_name": (char.get("name") or "").strip(),
                        "character_path": (char.get("path") or "").strip(),
                        "score": row.get("score"),
                        "score_color": (row.get("scoreColor") or "").strip(),
                        "rio_region_slug": (rio_region_obj.get("slug") or "").strip(),
                        "realm_slug": (realm_obj.get("slug") or "").strip(),
                        "realm_name": (realm_obj.get("name") or "").strip(),
                        "is_active": True,
                    },
                )

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
