# -*- coding: utf-8 -*-
"""
SpecDetail 采集器基类
封装 WCL GraphQL / Raider.IO / Battle.net API 调用
"""

import time


import requests
from django.conf import settings

from botend.controller.BaseScan import BaseScan

from utils.log import logger


class SpecDetailBase(BaseScan):
    """SpecDetail 系列采集器的共享基类"""

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self._wcl_token = None
        self._wcl_token_expire = 0
        # 代理配置
        req_cfg = getattr(settings, 'REQUEST_CONFIG', {})
        self._proxies = req_cfg.get('proxies') if req_cfg.get('enable_proxy', False) else None

    # ========== WCL v2 GraphQL ==========

    def _get_wcl_token(self):
        """获取 WCL OAuth access token（带缓存）"""
        if self._wcl_token and time.time() < self._wcl_token_expire:
            return self._wcl_token

        cfg = getattr(settings, 'WCL_V2_CONFIG', {})
        client_id = cfg.get('client_id')
        client_secret = cfg.get('client_secret')
        if not client_id or not client_secret:
            logger.error("[SpecDetail] WCL_V2_CONFIG 未配置")
            return None

        try:
            resp = requests.post(
                "https://www.warcraftlogs.com/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=20,
                proxies=self._proxies
            )
            if resp.status_code != 200:
                logger.error(f"[SpecDetail] WCL OAuth 失败: HTTP {resp.status_code}")
                return None
            data = resp.json()
            self._wcl_token = data.get('access_token')
            self._wcl_token_expire = time.time() + data.get('expires_in', 3600) - 60
            return self._wcl_token
        except Exception as e:
            logger.error(f"[SpecDetail] WCL OAuth 异常: {e}")
            return None

    def _wcl_graphql(self, query, variables, retries=3):
        """执行 WCL GraphQL 查询"""
        token = self._get_wcl_token()
        if not token:
            return None

        url = "https://www.warcraftlogs.com/api/v2/client"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        for attempt in range(retries):
            try:
                resp = requests.post(
                    url,
                    json={"query": query, "variables": variables},
                    headers=headers,
                    timeout=30,
                    proxies=self._proxies
                )
                if resp.status_code == 429:
                    wait = min(2 ** attempt * 2, 30)
                    logger.warning(f"[SpecDetail] WCL 429 限流，等待 {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.error(f"[SpecDetail] WCL HTTP {resp.status_code}")
                    if attempt < retries - 1:
                        time.sleep(1)
                        continue
                    return None

                payload = resp.json()
                if payload.get('errors'):
                    logger.error(f"[SpecDetail] WCL GraphQL 错误: {payload['errors']}")
                    return None
                return payload.get('data')
            except Exception as e:
                logger.error(f"[SpecDetail] WCL 请求异常 (attempt {attempt+1}): {e}")
                if attempt < retries - 1:
                    time.sleep(1)
        return None

    def fetch_wcl_rankings(self, encounter_id, class_name, spec_name, metric='dps', difficulty=None, page=1):
        """
        获取 WCL 角色排名
        返回 rankings dict: {count, hasMorePages, page, rankings: [...]}
        """
        diff_param = ""
        if difficulty:
            diff_param = f'difficulty: {difficulty},'

        query = f"""
        query {{
            worldData {{
                encounter(id: {encounter_id}) {{
                    characterRankings(
                        className: "{class_name}"
                        specName: "{spec_name}"
                        metric: {metric}
                        {diff_param}
                        page: {page}
                        includeCombatantInfo: true
                    )
                }}
            }}
        }}
        """
        data = self._wcl_graphql(query, {})
        if not data:
            return None

        rankings = data.get('worldData', {}).get('encounter', {}).get('characterRankings', {})
        if isinstance(rankings, dict) and rankings.get('error'):
            logger.error(f"[SpecDetail] WCL rankings error: {rankings['error']}")
            return None
        return rankings

    # ========== Raider.IO ==========

    def fetch_raiderio_top(self, class_name, spec_name, season, region='us', limit=20, page=1):
        """
        获取 Raider.IO 专精 Top N 玩家
        返回 list of character dicts
        """
        # Raider.IO 使用 slug 格式（小写连字符）
        class_slug = self._rio_class_slug(class_name)
        spec_slug = self._rio_spec_slug(spec_name)

        url = "https://raider.io/api/mythic-plus/rankings/specs"
        params = {
            "season": season,
            "region": region,
            "class": class_slug,
            "spec": spec_slug,
            "page": page,
            "pageSize": limit,
        }

        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=25, headers={"User-Agent": "Mozilla/5.0"}, proxies=self._proxies)
                if resp.status_code != 200:
                    logger.warning(f"[SpecDetail] Raider.IO HTTP {resp.status_code} for {class_name}/{spec_name}")
                    time.sleep(0.5 + attempt * 0.5)
                    continue
                return resp.json() or {}
            except Exception as e:
                logger.error(f"[SpecDetail] Raider.IO 异常: {e}")
                time.sleep(0.5)
        return {}

    def fetch_raiderio_character(self, region, realm, name):
        """获取 Raider.IO 单角色详情（装备、天赋等）"""
        url = "https://raider.io/api/v1/characters/profile"
        params = {
            "region": region.lower(),
            "realm": realm,
            "name": name,
            "fields": "gear,talents,mythic_plus_scores_by_season:current"
        }
        try:
            resp = requests.get(url, params=params, timeout=25, headers={"User-Agent": "Mozilla/5.0"}, proxies=self._proxies)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error(f"[SpecDetail] Raider.IO character 异常: {e}")
        return None

    # ========== Battle.net ==========

    def _get_battlenet_token(self):
        """获取 Battle.net OAuth token"""
        cfg = getattr(settings, 'BATTLENET_CONFIG', {})
        client_id = cfg.get('client_id')
        client_secret = cfg.get('client_secret')
        token_url = cfg.get('token_url', 'https://oauth.battle.net/token')

        if not client_id or not client_secret:
            return None

        try:
            resp = requests.post(
                token_url,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=20,
                proxies=self._proxies
            )
            if resp.status_code == 200:
                return resp.json().get('access_token')
        except Exception as e:
            logger.error(f"[SpecDetail] Battle.net OAuth 异常: {e}")
        return None

    def fetch_battlenet_stats(self, realm, character_name, region='us'):
        """获取 Battle.net 角色属性面板"""
        cfg = getattr(settings, 'BATTLENET_CONFIG', {})

        if region.lower() == 'cn':
            host = cfg.get('api_host_cn', 'https://gateway.battlenet.com.cn')
            namespace = 'profile-cn'
            locale = 'zh_CN'
        else:
            host = cfg.get('api_host_us', 'https://us.api.blizzard.com')
            namespace = 'profile-us'
            locale = 'en_US'

        token = self._get_battlenet_token()
        if not token:
            return None

        realm_slug = realm.lower().replace("'", "").replace(" ", "-")
        name_lower = character_name.lower()

        url = f"{host}/profile/wow/character/{realm_slug}/{name_lower}/statistics"
        params = {
            "namespace": namespace,
            "locale": locale,
        }
        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=25, proxies=self._proxies)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(f"[SpecDetail] Battle.net HTTP {resp.status_code} for {character_name}-{realm}")
        except Exception as e:
            logger.error(f"[SpecDetail] Battle.net 异常: {e}")
        return None

    def parse_battlenet_stats(self, data):
        """从 Battle.net response 提取属性数据"""
        if not data:
            return None

        result = {}

        # 基础属性
        health = data.get('health') or {}
        power = data.get('power') or {}
        if power:
            result['strength'] = power.get('strength', {}).get('effective')
            result['agility'] = power.get('agility', {}).get('effective')
            result['intellect'] = power.get('intellect', {}).get('effective')
            result['stamina'] = power.get('stamina', {}).get('effective')

        # 防御
        armor = data.get('armor') or {}
        if armor:
            result['armor'] = armor.get('effective')

        # 二级属性
        speed = data.get('speed', {}) or {}
        crit = data.get('crit', {}) or {}
        haste = data.get('haste', {}) or {}
        mastery = data.get('mastery', {}) or {}
        versatility = data.get('versatility', {}) or {}
        leech = data.get('leech', {}) or {}
        dodge = data.get('dodge', {}) or {}
        parry = data.get('parry', {}) or {}

        if crit:
            result['crit'] = {'rating': crit.get('rating'), 'pct': crit.get('value')}
        if haste:
            result['haste'] = {'rating': haste.get('rating'), 'pct': haste.get('value')}
        if mastery:
            result['mastery'] = {'rating': mastery.get('rating'), 'pct': mastery.get('value')}
        if versatility:
            result['versatility'] = {'rating': versatility.get('rating'), 'pct': versatility.get('damageDoneBonus')}
        if dodge:
            result['dodge'] = {'pct': dodge.get('value')}
        if parry:
            result['parry'] = {'pct': parry.get('value')}

        return result

    # ========== 工具方法 ==========

    def _rio_class_slug(self, class_name):
        """camelCase → raider.io slug（小写连字符）"""
        mapping = {
            "DeathKnight": "death-knight",
            "DemonHunter": "demon-hunter",
            "Druid": "druid",
            "Hunter": "hunter",
            "Mage": "mage",
            "Monk": "monk",
            "Paladin": "paladin",
            "Priest": "priest",
            "Rogue": "rogue",
            "Shaman": "shaman",
            "Warrior": "warrior",
            "Warlock": "warlock",
            "Evoker": "evoker",
        }
        return mapping.get(class_name, class_name.lower())

    def _rio_spec_slug(self, spec_name):
        """camelCase → raider.io slug"""
        mapping = {
            "BeastMastery": "beast-mastery",
            "Marksmanship": "marksmanship",
        }
        return mapping.get(spec_name, spec_name.lower())

    @staticmethod
    def parse_wcl_gear(gear_list):
        """解析 WCL gear 数据为标准化格式"""
        if not gear_list:
            return []
        result = []
        for g in gear_list:
            item = {
                'name': g.get('name', ''),
                'id': g.get('id'),
                'icon': g.get('icon', ''),
                'itemLevel': g.get('itemLevel'),
                'quality': g.get('quality', ''),
                'bonusIDs': g.get('bonusIDs', []),
            }
            gems = g.get('gems', [])
            if gems:
                item['gems'] = [{'id': gem.get('id'), 'itemLevel': gem.get('itemLevel')} for gem in gems]
            result.append(item)
        return result

    @staticmethod
    def parse_wcl_talents(talent_list):
        """解析 WCL talent 数据为标准化格式"""
        if not talent_list:
            return []
        return [{'talentID': t.get('talentID'), 'points': t.get('points', 0)} for t in talent_list]
