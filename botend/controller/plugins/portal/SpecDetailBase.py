# -*- coding: utf-8 -*-
"""
SpecDetail 采集器基类
封装 WCL GraphQL / Raider.IO / Battle.net API 调用
"""

import threading
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
        self._wcl_token_lock = threading.Lock()
        self._wcl_retry_at = None
        self._wcl_last_error = ''
        self._battlenet_token = None
        self._battlenet_token_expire = 0
        # 代理配置
        req_cfg = getattr(settings, 'REQUEST_CONFIG', {})
        self._proxies = req_cfg.get('proxies') if req_cfg.get('enable_proxy', False) else None

    # ========== WCL v2 GraphQL ==========

    def _get_wcl_token(self):
        """Serialize expiry checks and refreshes; token/expiry are one state."""
        with self._wcl_token_lock:
            return self._refresh_wcl_token()

    def _refresh_wcl_token(self):
        """Called with the token lock held, including the cached fast path."""
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
            logger.error(f"[SpecDetail] WCL OAuth 异常: {type(e).__name__}")
            return None

    def _wcl_graphql(self, query, variables, retries=3):
        """Return data/None; budget deferrals carry an aware _wcl_retry_at.

        ``retries`` stays source-compatible. The shared transport deliberately
        performs no in-place retries: schedulers own checkpoint/resume policy.
        """
        from botend.services.wcl_api_budget import graphql_request

        self._wcl_last_error = ''
        self._wcl_retry_at = None
        token = self._get_wcl_token()
        if not token:
            self._wcl_last_error = 'authentication'
            return None
        client_id = (getattr(settings, 'WCL_V2_CONFIG', {}) or {}).get('client_id')
        result = graphql_request(client_id, token, query, variables, proxies=self._proxies)
        self._wcl_last_error, self._wcl_retry_at = result.error, result.retry_at
        if result.error:
            # Never log credentials, raw request exceptions or upstream bodies.
            log = logger.warning if result.error in ('permission', 'budget_wait', 'rate_limit', 'admission_busy') else logger.error
            log(f"[SpecDetail] WCL {result.error}")
            return None
        return result.payload.get('data')

    def prepare_combatant_worker_factory(self):
        """Opt into concurrent details with fresh request/error state per batch.

        Custom/legacy fetchers remain serial unless they explicitly implement a
        factory. Never shallow-copy a monitor, bound override, or HTTP session.
        Only the canonical locked OAuth cache is shared; request/error state is
        private. Failed refreshes stop further OAuth attempts for this run.
        """
        for name in ('fetch_wcl_combatant_info', '_wcl_graphql', '_get_wcl_token', '_refresh_wcl_token'):
            method = getattr(self, name)
            canonical = getattr(SpecDetailBase, name)
            if (getattr(method, '__func__', None) is not canonical
                    or not hasattr(canonical, '__code__')):
                return None
        token = self._get_wcl_token()
        auth_failed = not token
        provider_lock = threading.Lock()
        proxies = dict(self._proxies) if self._proxies else None

        def shared_token():
            nonlocal auth_failed
            with provider_lock:
                if auth_failed:
                    return None
                current = self._get_wcl_token()
                auth_failed = not current
                return current

        def factory():
            worker = SpecDetailBase(None, None)
            worker._proxies = dict(proxies) if proxies else None
            worker._get_wcl_token = shared_token
            return worker
        return factory

    def _is_wcl_report_permission_error(self, errors):
        """WCL 私有日志/无权限日志是排名样本里的可预期缺口，不写后台错误记录。"""
        for error in errors or []:
            message = str((error or {}).get('message') or '')
            path = (error or {}).get('path') or []
            if (
                'permission to view this report' in message.lower()
                and len(path) >= 2
                and path[0] == 'reportData'
                and path[1] == 'report'
            ):
                return True
        return False

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
        """Fetch one report's bounded fight batch, including every events page.

        Scalar callers remain supported. None means failure (not an empty log).
        The source actor is retained verbatim with each raw CombatantInfo event.
        """
        self._wcl_last_error = ''
        self._wcl_retry_at = None
        if not report_code or not fight_id:
            return []
        try:
            fight_ids = sorted({int(value) for value in (
                fight_id if isinstance(fight_id, (list, tuple)) else [fight_id])})
            if not fight_ids or len(fight_ids) > 20 or min(fight_ids) <= 0:
                raise ValueError('invalid fight batch')
        except (TypeError, ValueError):
            self._wcl_last_error = 'invalid_input'
            return None
        query = """
        query($code:String!, $fightIDs:[Int], $start:Float) {
            reportData {
                report(code:$code) {
                    masterData { actors(type: "Player") { id name server type subType } }
                    events(fightIDs:$fightIDs, dataType: CombatantInfo,
                           limit: 300, startTime:$start) {
                        data nextPageTimestamp
                    }
                }
            }
        }
        """
        result, cursor = [], None
        for page in range(20):
            data = self._wcl_graphql(query, {'code': report_code, 'fightIDs': fight_ids, 'start': cursor}, retries=2)
            if not data:
                self._wcl_last_error = self._wcl_last_error or 'request_failed'
                return None
            report = (data.get('reportData') or {}).get('report')
            if not isinstance(report, dict):
                self._wcl_last_error = 'report_unavailable'
                return None
            actors = (report.get('masterData') or {}).get('actors')
            envelope = report.get('events')
            if not isinstance(actors, list) or not isinstance(envelope, dict) or not isinstance(envelope.get('data'), list):
                self._wcl_last_error = 'invalid_payload'
                return None
            actor_map = {actor.get('id'): actor for actor in actors if isinstance(actor, dict)}
            for raw in envelope['data']:
                if not isinstance(raw, dict):
                    self._wcl_last_error = 'invalid_payload'
                    return None
                event = dict(raw)
                actor = actor_map.get(event.get('sourceID'))
                if actor:
                    event['source'] = actor
                result.append(event)
            if 'nextPageTimestamp' not in envelope:
                self._wcl_last_error = 'invalid_pagination'
                return None
            next_cursor = envelope['nextPageTimestamp']
            if next_cursor is None:
                return result
            if not isinstance(next_cursor, (int, float)) or (cursor is not None and next_cursor <= cursor):
                self._wcl_last_error = 'invalid_pagination'
                return None
            cursor = next_cursor
        self._wcl_last_error = 'pagination_limit'
        return None

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
        """Keep WCL equipment, gems and enchantment IDs without guessing item IDs."""
        from botend.services.wcl_combatant_snapshot import combatant_gear
        return combatant_gear(gear_list)

    @staticmethod
    def parse_wcl_talents(talent_list):
        """解析 WCL talent 数据为标准化格式"""
        if not talent_list:
            return []
        result = []
        for t in talent_list:
            if not isinstance(t, dict):
                result.append({'source': 'wcl_log', 'invalid': True})
                continue
            entry_id = t.get('talentID') or t.get('talent_id') or t.get('id')
            result.append({
                'tree_type': t.get('treeType') or t.get('tree_type') or 'spec',
                'entry_id': entry_id,
                'talentID': entry_id,
                'spellID': t.get('spellID') or t.get('spell_id'),
                'talent_id': entry_id,
                'spell_id': t.get('spellID') or t.get('spell_id'),
                'node_id': entry_id,
                'db2_trait_node_id': t.get('nodeID') or t.get('node_id'),
                'source': 'wcl_log',
                'name': t.get('name', ''),
                'icon': t.get('icon', ''),
                'points': t.get('points', t.get('rank', 0)),
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
            if not isinstance(t, dict):
                result.append({'source': 'wcl_combatantinfo', 'invalid': True})
                continue
            entry_id = t.get('id')
            result.append({
                'tree_type': t.get('treeType') or t.get('tree_type') or 'spec',
                'talentID': entry_id,
                'entry_id': entry_id,
                'spellID': t.get('spellID') or t.get('spell_id'),
                'talent_id': entry_id,
                'spell_id': t.get('spellID') or t.get('spell_id'),
                'node_id': entry_id,
                'db2_trait_node_id': t.get('nodeID') or t.get('node_id'),
                'points': t.get('rank', 0),
                'rank': t.get('rank', 0),
                'row': t.get('row'),
                'column': t.get('column'),
                'name': t.get('name', ''),
                'icon': t.get('icon', ''),
                'source': 'wcl_combatantinfo',
            })
        return result
