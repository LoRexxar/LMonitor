# -*- coding: utf-8 -*-
"""
赛季元数据同步采集器
从 WCL 和 Raider.IO 获取当前赛季信息，更新 SeasonMeta 表
"""

import time


from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta

from utils.log import logger


class SpecDetailSeasonMonitor(SpecDetailBase):
    EXTRA_RAID_ZONE_IDS_BY_SEASON = {
        'mn-s1': [50],  # Sporefall / Rotmire is published as a separate WCL raid zone.
    }

    def __init__(self, req, task):
        super().__init__(req, task)

    def scan(self, url):
        logger.info("[SpecDetailSeason] 开始同步赛季元数据")

        # 1. 获取 WCL zones
        zones = self._fetch_wcl_zones()
        if not zones:
            logger.error("[SpecDetailSeason] 获取 WCL zones 失败")
            return False

        # 2. 找到最新的 M+ Season zone
        mplus_zone = self._find_latest_mplus_zone(zones)
        if not mplus_zone:
            logger.error("[SpecDetailSeason] 未找到 M+ Season zone")
            return False

        # 3. 获取 Raider.IO 赛季 slug，并确定 season_key
        # 从 rio_season 推断，如 "season-mn-1" → "mn-s1"；fallback 到旧逻辑
        rio_season = self._fetch_rio_season()
        mplus_name = mplus_zone.get('name', '') if mplus_zone else ''
        season_num = self._extract_season_number(mplus_name)
        season_key = self._derive_season_key(rio_season, season_num, mplus_zone)

        # 4. 找到所有 Raid zones
        raid_zones = self._find_all_raid_zones(zones, season_key=season_key)
        if not raid_zones:
            logger.warning("[SpecDetailSeason] 未找到 Raid zone")

        # 5. 获取副本/Boss 列表
        mplus_encounters = self._fetch_encounters(mplus_zone['id']) if mplus_zone else []
        all_raid_encounters = []
        for rz in raid_zones:
            encs = self._fetch_encounters(rz['id'])
            rz['encounters'] = encs
            all_raid_encounters.extend(encs)

        # 添加 index 到所有 raid encounters
        for i, enc in enumerate(all_raid_encounters):
            enc['index'] = i + 1

        # 6. 获取最新 wcl_partition
        wcl_partition = self._fetch_wcl_partition(mplus_zone['id']) if mplus_zone else 1

        # 7. 更新 SeasonMeta
        # 只同步/暂存新赛季元数据，不自动切换当前活跃赛季。
        # 新赛季必须由后台手动设置 is_active=1 后，专精详情页才会切换过去；
        # 否则刚发现的新赛季在 ranking/player 数据尚未采集完成时会让页面整体变空。
        existing = SeasonMeta.objects.filter(season_key=season_key).first()
        keep_active = bool(existing and existing.is_active)
        obj, created = SeasonMeta.objects.update_or_create(
            season_key=season_key,
            defaults={
                'season_name': mplus_name,
                'is_active': keep_active,
                'rio_season': rio_season,
                'wcl_partition': wcl_partition,
                'mplus_zone_id': mplus_zone['id'] if mplus_zone else 0,
                'mplus_zone_name': mplus_name,
                'raid_zone_id': raid_zones[-1]['id'] if raid_zones else 0,
                'raid_zone_name': raid_zones[-1].get('name', '') if raid_zones else '',
                'raid_zones': raid_zones,
                'mplus_encounters': mplus_encounters,
                'raid_encounters': all_raid_encounters,
            }
        )

        action = "创建" if created else "更新"
        logger.info(f"[SpecDetailSeason] {action} SeasonMeta: {season_key}, "
                     f"active={obj.is_active}, M+ {len(mplus_encounters)} 副本, Raid {len(raid_zones)} 区域 {len(all_raid_encounters)} Boss")

        self.task.flag = f"{season_key}@{int(time.time())}"
        self.task.save()
        return True

    def _fetch_wcl_zones(self):
        """获取所有 WCL zones"""
        query = '{ worldData { zones { id name } } }'
        data = self._wcl_graphql(query, {})
        if not data:
            return []
        return data.get('worldData', {}).get('zones', [])

    def _find_latest_mplus_zone(self, zones):
        """找到最新的 M+ Season zone"""
        mplus_zones = [z for z in zones if 'Mythic+ Season' in (z.get('name') or '')]
        if not mplus_zones:
            return None
        # 取 ID 最大的
        return max(mplus_zones, key=lambda z: z['id'])

    def _find_all_raid_zones(self, zones, season_key=None):
        """从数据库读取已配置的团本区域 ID，从 WCL zones 中匹配详情。
        
        注意：始终从 WCL zones 匹配，不读 raid_zones 缓存中的 encounter。
        因为旧赛季的 raid_zones 可能存了错误的 zone 数据（如 TWW zone），
        必须用配置的 zone id 从最新的 WCL zones 中重新匹配。
        """
        season = SeasonMeta.objects.filter(is_active=True).first()
        zone_ids = []
        if season:
            if season.raid_zone_id:
                zone_ids.append(season.raid_zone_id)
            if isinstance(season.raid_zones, list):
                for rz in season.raid_zones or []:
                    if not isinstance(rz, dict):
                        continue
                    zone_id = rz.get('id') or rz.get('zone_id')
                    if zone_id:
                        zone_ids.append(int(zone_id))
            elif season.raid_zones:
                logger.warning(
                    f"[SpecDetailSeason] SeasonMeta.raid_zones 格式异常，忽略缓存并使用 raid_zone_id: {type(season.raid_zones).__name__}"
                )
            season_key = season_key or season.season_key

        for zone_id in self.EXTRA_RAID_ZONE_IDS_BY_SEASON.get(season_key or '', []):
            zone_ids.append(zone_id)

        zone_ids = list(dict.fromkeys(zone_ids))
        if not zone_ids:
            return []
        
        zone_map = {z['id']: z for z in zones}
        raid_zones = []
        for zone_id in zone_ids:
            if zone_id in zone_map:
                raid_zones.append(zone_map[zone_id])
            else:
                logger.warning(f"[SpecDetailSeason] raid_zone_id={zone_id} 在 WCL zones 中未找到")
        return raid_zones

    def _fetch_encounters(self, zone_id):
        """获取 zone 下的所有 encounters"""
        query = f'{{ worldData {{ zone(id: {zone_id}) {{ encounters {{ id name }} }} }} }}'
        data = self._wcl_graphql(query, {})
        if not data:
            return []
        zone = data.get('worldData', {}).get('zone', {})
        encounters = zone.get('encounters', [])
        return [{'id': e['id'], 'name': e['name']} for e in encounters]

    def _fetch_rio_season(self):
        """从 Raider.IO 获取当前赛季 slug"""
        try:
            import requests
            resp = requests.get(
                "https://raider.io/api/v1/mythic-plus/static-data?expansion_id=11",
                timeout=25,
                headers={"User-Agent": "Mozilla/5.0"},
                proxies=self._proxies,
            )
            if resp.status_code == 200:
                seasons = (resp.json() or {}).get("seasons", [])
                if seasons:
                    return (seasons[0].get("slug") or "").strip()
        except Exception as e:
            logger.warning(f"[SpecDetailSeason] Raider.IO 赛季获取失败: {e}")
        return None

    def _extract_season_number(self, name):
        """从 'Mythic+ Season 3' 提取赛季号 3"""
        import re
        m = re.search(r'Season\s+(\d+)', name)
        return int(m.group(1)) if m else None

    def _derive_season_key(self, rio_season, season_num, mplus_zone):
        """从 rio_season slug 推断 season_key。

        rio_season 格式: 'season-{expansion}-{num}'，如 'season-mn-1'
        → 解析为 'mn-s1'。解析失败则 fallback 到旧逻辑。
        """
        import re
        if rio_season:
            m = re.match(r'^season-([a-z]+)-(\d+)$', rio_season)
            if m:
                expansion = m.group(1)
                num = m.group(2)
                return f"{expansion}-s{num}"
        # fallback
        if season_num:
            return f"tww-s{season_num}"
        return f"wcl-zone-{mplus_zone['id']}" if mplus_zone else "unknown"

    def _fetch_wcl_partition(self, zone_id):
        """从 WCL API 动态获取指定 zone 的最新 partition id"""
        query = f'{{ worldData {{ zone(id: {zone_id}) {{ partitions {{ id name }} }} }} }}'
        data = self._wcl_graphql(query, {})
        if not data:
            return 1
        zone = data.get('worldData', {}).get('zone', {})
        partitions = zone.get('partitions', [])
        if not partitions:
            return 1
        # 取最大 id（最新 partition）
        return max(p.get('id', 1) for p in partitions)
