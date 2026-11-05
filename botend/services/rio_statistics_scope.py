"""数据观察使用数据库赛季与正式服版本，不按上游日期猜测当前赛季。"""
import hashlib
import json
import re

from django.db import DatabaseError
from botend.models import SeasonMeta, WowTalentVersion
from botend.services.season_keys import canonical_season_key


class ScopeUnavailable(ValueError):
    pass


def active_scope():
    rows = list(SeasonMeta.objects.filter(is_active=True).order_by('pk').values(
        'id', 'season_key', 'season_name', 'rio_season', 'game_build',
        'mplus_encounters', 'raid_zones', 'raid_encounters')[:2])
    if len(rows) != 1:
        raise ScopeUnavailable('请在赛季管理中设置唯一的当前赛季。')
    row = rows[0]
    slug = (row['rio_season'] or '').strip()
    if not slug:
        key = canonical_season_key(row['season_key'])
        match = re.fullmatch(r'(mn|tww|df|sl|bfa)-s(\d+)', key)
        if match:
            slug = f'season-{match[1]}-{match[2]}'
    if not re.fullmatch(r'season-[a-z]+-\d+', slug):
        raise ScopeUnavailable('当前数据库赛季尚未配置 Raider.IO 赛季标识，请在赛季管理中补齐。')
    version = WowTalentVersion.objects.filter(branch='retail', is_active=True).only('id', 'key', 'current_build').order_by('-id')
    exact = version.filter(current_build=row['game_build']).first() if row['game_build'] else None
    current = exact or version.filter(key='retail').first() or version.first()
    build = row['game_build'] or (current.current_build if current else '')
    match = re.match(r'^(\d+)\.', build)
    if not match:
        raise ScopeUnavailable('当前赛季尚未绑定有效的正式服构建版本。')
    expansion_id = int(match[1]) - 1
    if expansion_id < 6:
        raise ScopeUnavailable('当前数据库版本不支持大秘境统计。')
    state = dict(row, rio_season=slug, game_build=build, expansion_id=expansion_id,
                 version_key=current.key if current else '', version_id=current.pk if current else None,
                 version_build=current.current_build if current else '')
    # 中文手册发布变化也会触发名称和地图绑定重建。
    from botend.journal_models import JournalState
    try:
        state['journal_release_id'] = JournalState.objects.filter(key='wow-zhCN').values_list('active_release_id', flat=True).first()
    except DatabaseError:
        state['journal_release_id'] = None
    state['token'] = hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return state


def public_scope(scope):
    result = {key: scope.get(key) for key in ('token', 'id', 'season_key', 'season_name', 'rio_season', 'game_build', 'version_build')}
    if not re.search(r'[\u3400-\u9fff]', result.get('season_name') or ''):
        match = re.fullmatch(r'season-([a-z]+)-(\d+)', scope.get('rio_season', ''))
        labels = {'mn': '午夜', 'tww': '地心之战', 'df': '巨龙时代', 'sl': '暗影国度', 'bfa': '争霸艾泽拉斯'}
        result['season_name'] = f'{labels.get(match[1], "魔兽世界")}第 {match[2]} 赛季' if match else '当前赛季'
    return result
