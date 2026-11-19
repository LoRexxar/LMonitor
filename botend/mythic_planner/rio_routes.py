"""从当季世界限时榜选择公开路线，并发布到 MDT 推荐路线。"""
import json
import re
import threading

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_IDENTITY_MAP
from botend.models import MythicDungeon, MythicDungeonDefaultRoute, MythicRouteTarget, SeasonMeta
from botend.mythic_planner.services import decode_share_code, validate_route_payload
from botend.services.raiderio_run_roster import _pace, validate_details

RIO = 'https://raider.io'
GURU = 'https://keystone.guru'
PAGE_SIZE = 40


def route_session():
    session = requests.Session()
    session.proxies.update(getattr(settings, 'PROXY_CONFIG', {}) or {})
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(429, 502, 503, 504), allowed_methods={'GET'})
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


class IncompleteRouteExport(ValueError):
    """上游已生成导出结果，但报告该条路线不完整。"""


class RouteSource:
    def __init__(self, session):
        self.session = session
        self.stop_event = threading.Event()

    def rio(self, path, **params):
        _pace(self.stop_event)
        response = self.session.get(RIO + path, params=params, timeout=25)
        response.raise_for_status()
        return response.json()

    def dungeons(self, season):
        catalog = self.rio('/api/v1/mythic-plus/static-data', expansion_id=11)
        now = timezone.now()
        active = []
        for item in catalog['seasons']:
            if not item.get('is_main_season'):
                continue
            for region, stamp in (item.get('starts') or {}).items():
                start = parse_datetime(stamp) if stamp else None
                end_stamp = (item.get('ends') or {}).get(region)
                end = parse_datetime(end_stamp) if end_stamp else None
                if start and start <= now and (end is None or now < end):
                    active.append((start, item['slug'], item))
        latest = max(active, key=lambda entry: entry[0])[2] if active else {}
        if latest.get('slug') != season or not latest.get('dungeons'):
            raise ValueError('站内当前赛季与 Raider.IO 生效主赛季不一致，保留已发布路线。')
        return latest['dungeons']

    def rankings(self, season, slug, level):
        page = 0
        last_rank = 0
        while True:
            result = self.rio('/api/mythic-plus/rankings/runs', season=season, region='world',
                              dungeon=slug, minMythicLevel=level, maxMythicLevel=level,
                              strict='true', affixes='all', page=page, limit=PAGE_SIZE,
                              eventId=0, faction='', realm='all', period=0, recent='false', search='')
            rankings = result['rankings']
            rows = rankings['rankedGroups']
            if not isinstance(rows, list):
                raise ValueError('Raider.IO 排名响应无效。')
            for row in rows:
                rank = row['rank']
                if rank <= last_rank:
                    raise ValueError('Raider.IO 排名分页重复或乱序。')
                last_rank = rank
                run = row['run']
                if (run['season'] != season or run['dungeon']['slug'] != slug
                        or (level and run['mythic_level'] != level)):
                    raise ValueError('Raider.IO 榜单筛选条件不匹配。')
                if run['num_chests'] > 0 and run.get('logged_run_id'):
                    yield run
            if len(rows) < PAGE_SIZE or page == (rankings.get('ui') or {}).get('lastPage'):
                return
            page += 1

    def export(self, slug, key):
        if not re.fullmatch(r'[A-Za-z0-9]+', key) or not re.fullmatch(r'[a-z0-9-]+', slug):
            raise ValueError('路线标识无效。')
        page = self.session.get(f'{GURU}/route/{slug}/{key}/embed', timeout=25)
        page.raise_for_status()
        match = re.search(r'"mdtExportUrl"\s*:\s*("(?:\\.|[^"\\])*")', page.text)
        if not match:
            raise ValueError('路线页未返回 MDT 导出地址。')
        path = json.loads(match[1])
        if not path.startswith(f'/ajax/{key}/mdtExport?'):
            raise ValueError('MDT 导出地址与路线不符。')
        response = self.session.get(GURU + path, timeout=25, headers={
            'Referer': page.url, 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
        response.raise_for_status()
        result = response.json()
        if result.get('warnings'):
            raise IncompleteRouteExport(f'MDT 导出存在警告：{result["warnings"]}')
        return result['mdt_string']

    def winner(self, season, slug, target):
        for run in self.rankings(season, slug, target.level):
            if target.spec_id and all((member.get('character', {}).get('spec') or {}).get('id') != target.spec_id
                                      for member in run['roster']):
                continue
            run_id = run['keystone_run_id']
            try:
                detail = self.rio('/api/v1/mythic-plus/run-details', season=season, id=run_id)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    continue
                raise
            logged = detail.get('logged_details') or {}
            if not logged.get('route_key') or logged.get('showing_route_authorized') is not True:
                continue
            url = f'{RIO}/mythic-plus-runs/{season}/{run_id}'
            facts = validate_details(detail, dict(run, url=url, dungeon=run['dungeon']['name']), season)
            if not facts:
                continue
            members = [member['character'] for member in facts['roster']]
            ids = [member['spec'].get('id') for member in members]
            if any(i not in SPEC_IDENTITY_MAP for i in ids) or (target.spec_id and target.spec_id not in ids):
                continue
            lineup = []
            for member, spec_id in zip(members, ids):
                cls, spec = SPEC_IDENTITY_MAP[spec_id]
                lineup.append(f"{member['name']}-{member['realm'].get('slug', '')}（{CLASS_CN[cls]}·{SPEC_CN[spec]}）")
            description = "阵容：" + '、'.join(lineup) + f"\n来源：{url}"
            return self.export(slug, logged['route_key']), facts['mythic_level'], description
        return None


@transaction.atomic
def publish(target, dungeon_info, winner):
    current = MythicRouteTarget.objects.select_for_update().get(pk=target.pk)
    if not current.is_active or (current.level, current.spec_id) != (target.level, target.spec_id):
        return
    slug = dungeon_info['slug']
    routes = MythicDungeonDefaultRoute.objects.filter(collection_target=current, source_dungeon_slug=slug)
    if winner is None:
        routes.filter(is_active=True).update(is_active=False)
        return
    code, level, description = winner
    payload = decode_share_code(code)
    dungeon = MythicDungeon.objects.select_related('data_version').get(
        key=payload['dungeon_key'], map_id=dungeon_info['challenge_mode_id'],
        is_active=True, data_version__is_active=True,
    )
    name = f'{dungeon.display_name} · {target.label}'
    payload.update(name=name, dungeon_level=level)
    data = dict(dungeon_id=dungeon.pk, name=name, dungeon_level=level, description=description,
                applicable_level=f'{level} 层', is_active=True,
                route_data=validate_route_payload(payload, dungeon).payload, source_code=code)
    route = routes.first()
    if route and all(getattr(route, k) == v for k, v in data.items()):
        return
    if not route:
        route = MythicDungeonDefaultRoute(collection_target=current, source_dungeon_slug=slug)
    else:
        route.revision += 1
    for field, value in data.items():
        setattr(route, field, value)
    route.display_updated_on = timezone.localdate()
    route.save()


def collect_routes():
    targets = list(MythicRouteTarget.objects.filter(is_active=True).order_by('pk'))
    if not targets:
        return
    season = SeasonMeta.objects.filter(is_active=True).values_list('rio_season', flat=True).first()
    if not season:
        raise ValueError('当前赛季缺少 Raider.IO 标识。')
    errors = []
    with route_session() as session:
        source = RouteSource(session)
        dungeons = source.dungeons(season)
        for target in targets:
            for dungeon in dungeons:
                try:
                    publish(target, dungeon, source.winner(season, dungeon['slug'], target))
                except (ValueError, KeyError, requests.RequestException, MythicDungeon.DoesNotExist) as exc:
                    # 导出地址含短期签名，错误信息只保留路径。
                    errors.append(f"{target.label}/{dungeon['slug']}：{str(exc).split('?')[0]}")
    if errors:
        raise ValueError('；'.join(errors))
