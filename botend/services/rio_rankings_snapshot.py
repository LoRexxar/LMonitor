"""Raider.IO 纪录与巅峰榜统一发布服务；公开请求只读文件。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

from botend.models import PortalMplusRun, PortalPeakSpecRankRow, RaiderIORunRoster, SeasonMeta
from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON, SPEC_ROLE, canonical_class_spec
from botend.wow_i18n import cn_dungeon_from_slug
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write
from botend.services.character_navigation import character_link
from botend.services.raiderio_run_roster import _profile_url, run_reference, validate_details
from botend.raiderio_identity import normalized_name


MODULES = ('mplus', 'peak')
SCHEMA = 1


def snapshot_root():
    return Path(getattr(settings, 'RIO_RANKINGS_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var/rio-rankings'))


def _manifest():
    data = _load(snapshot_root() / 'index.json')
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or not isinstance(data.get('entries'), dict):
        return {'schema': SCHEMA, 'current_season': '', 'entries': {}}
    return data


def _key(module, season, region):
    return hashlib.sha256(json.dumps([module, season, region]).encode()).hexdigest()


def _fmt_dt(dt):
    return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S') if dt else ''


def _normalize_url(value):
    value = (value or '').strip()
    if value in ('-', '#'):
        return ''
    if value.startswith('/static/portal/reports/'):
        return '/portal/reports/' + value[len('/static/portal/reports/'):]
    return value


def _mplus_member_to_dict(member):
    """职业与专精联合确定中文名及图标，兼容已保存队伍。"""
    identity = canonical_class_spec(member.get('class_slug'), member.get('spec_slug'))
    if not identity:
        identity = canonical_class_spec(member.get('class'), member.get('spec'))
    if not identity:
        return {**member, 'spec_icon_url': ''}
    class_name, spec_name = identity
    return {**member, 'class_name_cn': CLASS_CN.get(class_name, class_name),
            'spec_name_cn': SPEC_CN.get(spec_name, spec_name),
            'spec_icon_url': SPEC_ICON.get(identity, '').replace('/small/', '/large/')}


def _mplus_to_dict(row):
    def parsed(value):
        try:
            return json.loads(value or '[]') or []
        except (TypeError, ValueError):
            return []
    party = parsed(getattr(row, 'party_json', None))
    party = [_mplus_member_to_dict(member) for member in party if isinstance(member, dict)] if isinstance(party, list) else []
    return {'rank': row.rank, 'dungeon': row.dungeon, 'dungeon_slug': row.dungeon_slug or '',
            'dungeon_cn': cn_dungeon_from_slug(row.dungeon_slug or '', row.dungeon),
            'level': row.level, 'time_seconds': row.time_seconds, 'score': row.score,
            'tank': row.tank or '', 'healer': row.healer or '', 'party': party, 'dps': parsed(row.dps_json),
            'run_url': _normalize_url(row.run_url), 'source': row.source or '',
            'season': row.season or '', 'region': row.region or ''}


def _peak_row_to_dict(row):
    identity = canonical_class_spec(getattr(row, 'class_slug', ''), getattr(row, 'spec_slug', ''))
    aggregate_url = f'/portal/spec/{identity[0]}/{identity[1]}/dungeons/' if identity else ''
    return {'rank': int(row.rank or 0), 'name': (row.character_name or '').strip(),
            'score': row.score, 'score_color': (row.score_color or '').strip(), 'aggregate_url': aggregate_url,
            'realm_name': (row.realm_name or '').strip(), 'rio_region_slug': (row.rio_region_slug or '').strip()}


def _enrich_mplus_parties(rows, season):
    """Join only this season's exact runs; keep source party facts untouched."""
    references = {}
    for row in rows:
        if row.source != 'raiderio':
            continue
        try:
            url = row.run_url or ''
            if url != url.strip() or any(ord(c) < 32 for c in url):
                continue
            parsed = urlsplit(url)
            # Older collectors stored a real run ID without a season segment.
            # The DB season is the scope, never a guessed current season.
            legacy = re.fullmatch(r'/mythic-plus-runs/([1-9][0-9]*)', parsed.path)
            if legacy and parsed.scheme == 'https' and parsed.netloc in ('raider.io', 'www.raider.io') and not parsed.query and not parsed.fragment:
                url = f'https://raider.io/mythic-plus-runs/{season}/{legacy[1]}'
            run = {'url': url, 'dungeon': row.dungeon, 'mythic_level': row.level}
            references[row.pk] = (run_reference(run, season), run)
        except (ValueError, TypeError):
            continue
    rosters = {row.run_id: row for row in RaiderIORunRoster.objects.filter(
        rio_season=season, run_id__in={key for key, _ in references.values()}, roster_visible=True)}
    result = {}
    for row in rows:
        projected = _mplus_to_dict(row)
        result[row.pk] = projected
        key, run = references.get(row.pk, (None, None))
        roster = rosters.get(key)
        if not roster:
            continue
        try:
            facts = validate_details(roster.payload_json, run, season)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        matches = {}
        for member in (facts or {}).get('roster', []):
            character = member['character']
            cls, spec = character['class'], character['spec']
            identity = canonical_class_spec(cls.get('slug') or cls.get('name'), spec.get('slug') or spec.get('name'))
            if identity:
                matches.setdefault((normalized_name(character['name']), identity), []).append(_profile_url(character))
        for member in projected['party']:
            if member.get('profile_url') or member.get('path'):
                continue
            identity = (canonical_class_spec(member.get('class_slug'), member.get('spec_slug'))
                        or canonical_class_spec(member.get('class'), member.get('spec')))
            candidates = matches.get((normalized_name(member.get('name', '')), identity), [])
            if len(candidates) == 1 and candidates[0]:
                member['profile_url'] = candidates[0]
    return result


def _build(module, season, region):
    if module == 'mplus':
        grouped = {}
        for row in PortalMplusRun.objects.filter(is_active=True, season=season, region=region).exclude(
                dungeon_slug__isnull=True).exclude(dungeon_slug='').order_by('dungeon_slug', 'rank', 'id').iterator():
            grouped.setdefault(row.dungeon_slug, []).append(row)
        selected = {row.pk: row for rows in grouped.values() for row in
                    [*rows[:30], min(rows, key=lambda row: (-row.level, row.time_seconds, row.id))]}
        projected = _enrich_mplus_parties(selected.values(), season)
        dungeons, best, runs = [], [], {}
        for slug, rows in grouped.items():
            name = cn_dungeon_from_slug(slug, rows[0].dungeon)
            dungeons.append({'slug': slug, 'name_cn': name})
            # 排名列表按排名；汇总仍按层数最高、耗时最短选取。
            runs[slug] = [projected[row.pk] for row in rows[:30]]
            best.append({**projected[min(rows, key=lambda row: (-row.level, row.time_seconds, row.id)).pk], 'dungeon_cn': name})
        return {'dungeons': dungeons, 'items': best, 'runs_by_dungeon': runs}
    groups = {}
    for row in PortalPeakSpecRankRow.objects.filter(is_active=True, season=season, region=region).order_by(
            'class_slug', 'spec_slug', 'rank', 'id').iterator():
        key = (row.class_slug.strip(), row.spec_slug.strip())
        identity = canonical_class_spec(*key)
        # The stored slugs identify the requested ranking scope. spec_name and
        # spec_role come from character.spec: the player's current spec can be
        # different or absent even in a valid per-spec ranking. Never infer the
        # ranking role from that profile metadata or a majority of its rows.
        role = SPEC_ROLE.get(identity) if identity else None
        if not identity or role not in ('tank', 'healer', 'dps'):
            raise ValueError(f'无法确定榜单专精职责：{key[0]}/{key[1]}，保留旧版')
        if key not in groups:
            groups[key] = {'class_slug': key[0], 'spec_slug': key[1],
                           'class_name': identity[0], 'spec_name': identity[1],
                           'aggregate_url': f'/portal/spec/{identity[0]}/{identity[1]}/dungeons/',
                           'spec_role': role, 'items': [], 'updated_at': _fmt_dt(row.updated_at)}
        groups[key]['items'].append(_peak_row_to_dict(row))
    return {'role': 'all', 'items': list(groups.values())}


def publish_rankings(module, *, season='', region='world', error=''):
    """定时采集后和显式离线预热共用；不由公开接口调用。"""
    if module not in MODULES:
        raise ValueError('未知榜单类型')
    active = SeasonMeta.objects.filter(is_active=True).values_list('rio_season', flat=True).first() or ''
    season = (season or active).strip()
    region = (region or 'world').strip()
    if not season or season == 'unknown':
        raise ValueError('缺少有效赛季，无法发布')
    root = snapshot_root()
    with _lock(root / 'publish.lock'):
        payload = _build(module, season, region)
        if not payload['items']:
            raise ValueError('榜单为空，保留上次发布')
        manifest = deepcopy(_manifest())
        key = _key(module, season, region)
        old = manifest['entries'].get(key, {})
        coordinate = [module, season, region]
        body = {'schema': SCHEMA, 'coordinate': coordinate, 'season': season, 'region': region, **payload}
        content_hash = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        previous = _published(old, coordinate)
        if previous and old.get('content_hash') == content_hash:
            entry = {**old, 'checked_at': timezone.now().isoformat()}
        else:
            body['generated_at'] = timezone.now().isoformat()
            filename = uuid.uuid4().hex + '.json'
            _write(root / filename, body)
            entry = {'file': filename, 'content_hash': content_hash, 'generated_at': body['generated_at']}
        if error:
            entry['error'] = error
        else:
            entry.pop('error', None)
        manifest['entries'][key] = entry
        if season == active:
            manifest['current_season'] = season
        from botend.services.snapshot_cache_gc import publish_snapshot_index
        publish_snapshot_index('rio', root, manifest, write_index=_write)
        return entry


def mark_failure(module, *, season, region='world', message='本次更新未成功，正在展示上次数据。'):
    """只更新已有范围的失败状态，保留正文和当前赛季。"""
    with _lock(snapshot_root() / 'publish.lock'):
        manifest = deepcopy(_manifest())
        key = _key(module, season, region)
        if key in manifest['entries']:
            manifest['entries'][key]['error'] = message
            from botend.services.snapshot_cache_gc import publish_snapshot_index
            publish_snapshot_index('rio', snapshot_root(), manifest, write_index=_write)


def _published(entry, coordinate):
    filename = entry.get('file', '')
    if not isinstance(filename, str) or not re.fullmatch(r'[0-9a-f]{32}\.json', filename):
        return None
    data = _load(snapshot_root() / filename)
    return data if (isinstance(data, dict) and data.get('schema') == SCHEMA
                    and data.get('coordinate') == coordinate and isinstance(data.get('items'), list)) else None


def _character_party(run):
    party = []
    for member in run.get('party', []):
        url = member.get('profile_url', '')
        path = member.get('path', '')
        if not url and isinstance(path, str) and path.startswith('/characters/'):
            url = 'https://raider.io' + path
        party.append({**member, 'character_url': character_link(url=url)})
    return {**run, 'party': party}


def read_rankings(module, *, season='', region='world', dungeon='', role='', catalog=False):
    """公开读取只使用已发布文件，筛选不登记任务或回源。"""
    manifest = _manifest()
    season = (season or '').strip()
    if season in ('', 'auto', 'season-mn-1'):
        season = manifest.get('current_season', '')
    region = (region or 'world').strip()
    entry = manifest['entries'].get(_key(module, season, region), {})
    published = _published(entry, [module, season, region])
    state = 'stale' if published and entry.get('error') else 'ready' if published else 'pending'
    result = {'season': season, 'region': region, 'items': [],
              'snapshot': {'state': state, 'generated_at': (published or {}).get('generated_at'),
                           'message': entry.get('error', '') if state == 'stale' else
                                      '该范围的数据尚未准备好，请稍后查看。' if state == 'pending' else ''}}
    if module == 'mplus':
        dungeon = (dungeon or '').strip()
        result['dungeons'] = (published or {}).get('dungeons', [])
        runs = (published or {}).get('runs_by_dungeon', {})
        result['items'] = [_character_party(run) for run in
                           (runs.get(dungeon, []) if dungeon else (published or {}).get('items', []))]
        if catalog:
            result['runs_by_dungeon'] = {slug: [_character_party(run) for run in rows] for slug, rows in runs.items()}
    else:
        role = (role or '').strip().lower()
        role = role if role in ('tank', 'healer', 'dps') else 'all'
        result['role'] = role
        result['items'] = [{**group, 'items': [{**row, 'character_url': character_link(
            region=row.get('rio_region_slug', ''), realm=row.get('realm_name', ''), name=row.get('name', ''))}
            for row in group.get('items', [])]} for group in (published or {}).get('items', [])
            if role == 'all' or group['spec_role'] == role]
    return result
