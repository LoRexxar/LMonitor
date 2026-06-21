# -*- coding: utf-8 -*-
"""
SpecDetail 采集器基类
封装 WCL GraphQL / Raider.IO / Battle.net API 调用
"""

import time
from urllib.parse import quote


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
        self._battlenet_token = None
        self._battlenet_token_expire = 0
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

    def fetch_wcl_combatant_info(self, report_code, fight_id):
        """获取单场日志的 CombatantInfo 事件，用于补充完整天赋节点。"""
        if not report_code or not fight_id:
            return []

        query = """
        query($code:String!, $fightIDs:[Int]) {
            reportData {
                report(code:$code) {
                    masterData {
                        actors {
                            id
                            name
                            server
                            type
                            subType
                        }
                    }
                    events(fightIDs:$fightIDs, dataType: CombatantInfo, limit: 300) {
                        data
                    }
                }
            }
        }
        """
        data = self._wcl_graphql(query, {
            'code': report_code,
            'fightIDs': [int(fight_id)],
        })
        report = (((data or {}).get('reportData') or {}).get('report') or {})
        actors = ((report.get('masterData') or {}).get('actors') or [])
        actor_map = {actor.get('id'): actor for actor in actors if actor.get('id') is not None}
        events = (report.get('events') or {}).get('data') or []
        for event in events:
            actor = actor_map.get(event.get('sourceID'))
            if actor:
                event['source'] = actor
        return events

    # ========== Raider.IO ==========

    def fetch_raiderio_top(self, class_name, spec_name, season, region='us', limit=20, page=0):
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
        if self._battlenet_token and time.time() < self._battlenet_token_expire:
            return self._battlenet_token

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
                payload = resp.json()
                access_token = payload.get('access_token')
                expires_in = int(payload.get('expires_in') or 0)
                if access_token:
                    self._battlenet_token = access_token
                    self._battlenet_token_expire = time.time() + max(60, expires_in - 60)
                    return access_token
        except Exception as e:
            logger.error(f"[SpecDetail] Battle.net OAuth 异常: {e}")
        return None

    def fetch_battlenet_stats(self, realm, character_name, region='us'):
        """获取 Battle.net 角色属性面板"""
        cfg = getattr(settings, 'BATTLENET_CONFIG', {})
        region = (region or 'us').lower()
        region_config = {
            'us': (cfg.get('api_host_us', 'https://us.api.blizzard.com'), 'profile-us', 'en_US'),
            'eu': (cfg.get('api_host_eu', 'https://eu.api.blizzard.com'), 'profile-eu', 'en_GB'),
            'kr': (cfg.get('api_host_kr', 'https://kr.api.blizzard.com'), 'profile-kr', 'ko_KR'),
            'tw': (cfg.get('api_host_tw', 'https://tw.api.blizzard.com'), 'profile-tw', 'zh_TW'),
            'cn': (cfg.get('api_host_cn', 'https://gateway.battlenet.com.cn'), 'profile-cn', 'zh_CN'),
        }
        host, namespace, locale = region_config.get(region, region_config['us'])

        token = self._get_battlenet_token()
        if not token:
            return None

        realm_slug = quote(realm.lower().replace("'", "").replace(" ", "-"), safe='-')
        name_lower = quote(character_name.lower(), safe='-')

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

        def _effective_value(value):
            if isinstance(value, dict):
                return value.get('effective') or value.get('base') or value.get('value')
            return value

        def _rating_pct(value, pct_key='value', rating_key='rating_normalized'):
            if isinstance(value, dict):
                return {
                    'rating': value.get(rating_key) or value.get('rating'),
                    'pct': value.get(pct_key),
                }
            if isinstance(value, (int, float)):
                return {'rating': None, 'pct': value}
            return None

        # 基础属性：兼容旧版 power 嵌套和新版顶层字段
        power = data.get('power') or {}
        result['strength'] = _effective_value(data.get('strength')) or _effective_value(power.get('strength'))
        result['agility'] = _effective_value(data.get('agility')) or _effective_value(power.get('agility'))
        result['intellect'] = _effective_value(data.get('intellect')) or _effective_value(power.get('intellect'))
        result['stamina'] = _effective_value(data.get('stamina')) or _effective_value(power.get('stamina'))

        # 防御/生命
        health = data.get('health')
        if isinstance(health, (int, float)):
            result['health'] = health
        elif isinstance(health, dict):
            result['health'] = health.get('max') or health.get('effective') or health.get('value')

        armor = data.get('armor') or {}
        if isinstance(armor, dict):
            result['armor'] = armor.get('effective') or armor.get('base')

        # 二级属性：兼容旧版 crit/haste 和新版 melee_crit/melee_haste
        crit = _rating_pct(data.get('melee_crit') or data.get('crit'))
        haste = _rating_pct(data.get('melee_haste') or data.get('haste'))
        mastery = _rating_pct(data.get('mastery'))
        versatility = _rating_pct(data.get('versatility'), pct_key='damageDoneBonus')
        if versatility:
            if versatility.get('rating') is None and isinstance(data.get('versatility'), (int, float)):
                versatility['rating'] = data.get('versatility')
            versatility_pct = data.get('versatility_damage_done_bonus')
            if versatility_pct is not None:
                versatility['pct'] = versatility_pct
        dodge = _rating_pct(data.get('dodge'))
        parry = _rating_pct(data.get('parry'))

        if crit and crit.get('pct') is not None:
            result['crit'] = crit
        if haste and haste.get('pct') is not None:
            result['haste'] = haste
        if mastery and mastery.get('pct') is not None:
            result['mastery'] = mastery
        if versatility and versatility.get('pct') is not None:
            result['versatility'] = versatility
        if dodge and dodge.get('pct') is not None:
            result['dodge'] = dodge
        if parry and parry.get('pct') is not None:
            result['parry'] = parry

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
                'slot': g.get('slot', 'unknown'),
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
        result = []
        for t in talent_list:
            result.append({
                'tree_type': t.get('treeType') or t.get('tree_type') or 'spec',
                'talentID': t.get('talentID'),
                'spellID': t.get('spellID') or t.get('talentID'),
                'talent_id': t.get('talentID'),
                'spell_id': t.get('spellID') or t.get('talentID'),
                'name': t.get('name', ''),
                'icon': t.get('icon', ''),
                'points': t.get('points', 0),
                'row': t.get('row'),
                'column': t.get('column'),
            })
        return result

    @staticmethod
    def parse_wcl_talent_tree(talent_tree):
        """解析 WCL CombatantInfo.talentTree 为标准化格式。"""
        if not talent_tree:
            return []
        result = []
        for t in talent_tree:
            result.append({
                'tree_type': t.get('treeType') or t.get('tree_type') or 'spec',
                'talentID': t.get('id'),
                'spellID': t.get('spellID') or t.get('id'),
                'talent_id': t.get('id'),
                'spell_id': t.get('spellID') or t.get('id'),
                'node_id': t.get('nodeID') or t.get('node_id'),
                'points': t.get('rank', 0),
                'rank': t.get('rank', 0),
                'row': t.get('row'),
                'column': t.get('column'),
                'name': t.get('name', ''),
                'icon': t.get('icon', ''),
                'source': 'wcl_combatantinfo',
            })
        return result
