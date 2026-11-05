"""数据观察名称：优先中央中文资料，再使用有来源的固定中文兜底。"""
import re
from django.db import DatabaseError

from botend.constants.wow import RAID_ZONE_CN
from botend.wow_i18n import cn_dungeon_from_slug, cn_class_from_slug, cn_spec_from_class_spec


def chinese(value):
    return bool(re.search(r'[\u3400-\u9fff]', str(value or '')))


def name_overrides(scope, names):
    """只查询本批英文词条，复用正式服名称资料，不建立第二份管理词库。"""
    result = {}
    if not names:
        return result
    try:
        from botend.services.wow_localization import effective_names
        records = effective_names(scope.get('version_key') or 'retail', source_text='\n'.join(names), name_candidates=names)
        for row in records:
            if row.get('name_en') in names and chinese(row.get('name_zh')):
                result[row['name_en']] = row['name_zh']
    except DatabaseError:
        pass
    return result


def localize_catalog(data, scope):
    from botend.journal_models import JournalInstance
    from botend.services.rio_statistics_scope import public_scope
    for row in data.get('raids', []):
        row.setdefault('source_name', row.get('name', ''))
    for season in data.get('seasons', []):
        for dungeon in season.get('dungeons', []):
            dungeon.setdefault('source_name', dungeon.get('name', ''))
    candidates = {row.get('source_name', row.get('name', '')) for row in data.get('raids', [])}
    for season in data.get('seasons', []):
        candidates.update(d.get('source_name', d.get('name', '')) for d in season.get('dungeons', []))
    active = next((s for s in data.get('seasons', []) if s['slug'] == scope['rio_season']), {})
    current_floor_ids = [str(d['challenge_mode_id']) for d in active.get('dungeons', [])]
    source_floors = [f for identity in current_floor_ids for f in data.get('floors', {}).get(identity, [])]
    candidates.update(f.get('name', '') for f in source_floors)
    overrides = name_overrides(scope, candidates)
    journal_by_map = {}
    if scope.get('journal_release_id'):
        try:
            for row in JournalInstance.objects.filter(release_id=scope['journal_release_id']).values('name', 'payload'):
                if chinese(row['name']):
                    payload = row['payload'] or {}
                    map_id = payload.get('map_id') or payload.get('mapID')
                    if map_id:
                        journal_by_map[int(map_id)] = row['name']
        except (DatabaseError, TypeError, ValueError):
            pass
    for row in data.get('raids', []):
        original = row.get('source_name', row.get('name', ''))
        aggregate = re.fullmatch(r'MN Tier (\d+) \(VS / DR / MQD\)', original)
        aggregate_name = f'午夜第 {aggregate[1]} 赛季团本' if aggregate else ''
        row['name'] = (overrides.get(original) or journal_by_map.get(row.get('id'))
                       or RAID_ZONE_CN.get(original) or aggregate_name
                       or (row.get('name') if chinese(row.get('name')) else '') or '团队副本')
    for season in data.get('seasons', []):
        if season['slug'] == scope['rio_season']:
            season['name'] = public_scope(scope)['season_name']
        for dungeon in season.get('dungeons', []):
            name = overrides.get(dungeon.get('source_name', '')) or cn_dungeon_from_slug(dungeon['slug'])
            dungeon['name'] = name if chinese(name) else '地下城'
    source_names = {str(f['id']): f.get('name', '') for f in source_floors}
    for atlas in data.get('maps', {}).values():
        for identity, floor in atlas.get('floors', {}).items():
            translated = overrides.get(source_names.get(identity))
            if translated:
                floor['name'] = translated
    return data


def class_name(slug):
    name = cn_class_from_slug(slug)
    return name if chinese(name) else '未知职业'


def spec_name(class_slug, spec_slug):
    name = cn_spec_from_class_spec(class_slug, spec_slug)
    return name if chinese(name) else '未知专精'
