"""统计文件按范围发布；概览与详情共用一个原子替换的入口。"""
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import re
import uuid

from botend.services.simc_benchmark_result_snapshot import _load


SCHEMA = 1


def content_digest(value):
    """Canonical content hash using the publisher's JSON scalar encoding."""
    def encode(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(f'Unsupported snapshot scalar: {type(obj).__name__}')
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False,
                                    default=encode).encode('utf-8')).hexdigest()


def _summary(detail):
    """保留概览卡片全部指标，剥离天赋、装备与人物明细。"""
    fields = ('dungeon_id', 'dungeon_name', 'boss_id', 'boss_name', 'name', 'short_name',
              'sample_size', 'dps', 'keystone', 'clear_time', 'kill_time',
              'faction_distribution', 'updated_at', 'last_updated')
    return {key: detail[key] for key in fields if key in detail}


def publish_projection(path, payload, write_json):
    """先写完整不可变详情，再发布轻量入口；中途失败保留原入口。"""
    from botend.services.spec_overview_service import SpecOverviewService

    path = Path(path)
    module = path.stem
    if module not in ('dungeon', 'raid'):
        write_json(path, payload)
        return
    generation = uuid.uuid4().hex
    scope = list(path.parent.parts[-3:])
    details = []
    detail_hashes = {}

    def save(key, detail):
        if not re.fullmatch(r'(dungeon-(all|\d+)|raid-[45]-\d+)', key) or key in details:
            raise ValueError('统计详情范围缺失或重复')
        if not isinstance(detail, dict):
            raise ValueError('统计详情不是有效对象')
        write_json(path.parent / 'stats-details' / generation / f'{key}.json', {
            'schema': SCHEMA, 'generation': generation, 'scope': scope,
            'key': key, 'data': detail,
        })
        details.append(key)
        detail_hashes[key] = content_digest(detail)

    index = {'generated_at': payload.get('generated_at'),
             'updated_at': payload.get('updated_at') or SpecOverviewService._latest_timestamp(
                 payload.get('dungeons') if module == 'dungeon' else payload.get('zone_groups'))
                 or payload.get('generated_at')}
    if module == 'dungeon':
        index['dungeons'] = []
        for detail in payload.get('dungeons', []):
            save(f"dungeon-{detail.get('dungeon_id')}", detail)
            index['dungeons'].append(_summary(detail))
        if payload.get('summary') is not None:
            save('dungeon-all', payload['summary'])
    else:
        difficulties = payload.get('difficulties') or [
            {'difficulty': 5, 'zone_groups': payload.get('zone_groups', [])}]
        index['difficulties'] = []
        for item in difficulties:
            difficulty = item['difficulty']
            if difficulty not in (4, 5):
                raise ValueError('统计团本难度无效')
            zones = []
            for zone in item.get('zone_groups', []):
                compact = {key: zone[key] for key in ('zone_id', 'zone_name', 'zone_cn', 'name') if key in zone}
                compact['bosses'] = []
                for detail in zone.get('bosses', []):
                    save(f"raid-{difficulty}-{detail.get('boss_id')}", detail)
                    compact['bosses'].append(_summary(detail))
                zones.append(compact)
            index['difficulties'].append({'difficulty': difficulty, 'label': item.get('label'), 'zone_groups': zones})
        index['zone_groups'] = next((row['zone_groups'] for row in index['difficulties'] if row['difficulty'] == 5), [])
    index['_snapshot'] = {'schema': SCHEMA, 'module': module, 'generation': generation,
                          'scope': scope, 'details': details}
    if payload.get('_input_fingerprint'):
        index['_snapshot'].update(input_fingerprint=payload['_input_fingerprint'],
                                  detail_hashes=detail_hashes)
    write_json(path, index)


def reusable_projection(path, fingerprint, required_keys):
    """Validate the captured generation, including every required detail body.

    A readable overview alone is not evidence of a complete publication. No
    mutable timestamp is refreshed here and legacy generations rebuild once.
    """
    path = Path(path)
    index = read_projection(path)
    meta = index.get('_snapshot', {}) if isinstance(index, dict) else {}
    if (meta.get('input_fingerprint') != fingerprint
            or meta.get('details') != list(required_keys)
            or not isinstance(meta.get('detail_hashes'), dict)):
        return None
    for key in required_keys:
        if not isinstance(key, str) or not re.fullmatch(r'(dungeon-(all|\d+)|raid-[45]-\d+)', key):
            return None
        envelope = _load(path.parent / 'stats-details' / meta['generation'] / f'{key}.json')
        if (not isinstance(envelope, dict) or envelope.get('schema') != SCHEMA
                or envelope.get('generation') != meta['generation']
                or envelope.get('scope') != meta['scope'] or envelope.get('key') != key
                or not isinstance(envelope.get('data'), dict)):
            return None
        try:
            if content_digest(envelope['data']) != meta['detail_hashes'].get(key):
                return None
        except (TypeError, ValueError):
            return None
    return index


def _valid_index_containers(payload, module):
    """合法 JSON 仍可能结构损坏；概览与详情都不能消费这样的入口。"""
    def valid_rows(rows, identity):
        return isinstance(rows, list) and all(
            isinstance(row, dict) and re.fullmatch(r'\d+', str(row.get(identity, '')))
            for row in rows
        )

    def valid_zones(zones):
        return isinstance(zones, list) and all(
            isinstance(zone, dict) and valid_rows(zone.get('bosses'), 'boss_id')
            for zone in zones
        )

    if module == 'dungeon':
        return valid_rows(payload.get('dungeons'), 'dungeon_id')
    if module == 'raid':
        difficulties = payload.get('difficulties')
        return (isinstance(difficulties, list) and all(
            isinstance(item, dict) and item.get('difficulty') in (4, 5)
            and valid_zones(item.get('zone_groups')) for item in difficulties
        ) and valid_zones(payload.get('zone_groups')))
    return False


def read_projection(path, detail_key=None):
    """旧完整文件仍只读兼容，新版只读取入口和一个所选详情。"""
    path = Path(path)
    payload = _load(path)
    if not isinstance(payload, dict):
        return None
    if '_snapshot' not in payload:
        return payload
    meta = payload['_snapshot']
    if (not isinstance(meta, dict) or meta.get('schema') != SCHEMA
            or meta.get('module') != path.stem or meta.get('scope') != list(path.parent.parts[-3:])
            or not re.fullmatch(r'[0-9a-f]{32}', str(meta.get('generation', '')))
            or not isinstance(meta.get('details'), list)
            or not _valid_index_containers(payload, path.stem)):
        return None
    if detail_key is None:
        return payload
    detail = None
    if (isinstance(detail_key, str) and re.fullmatch(r'(dungeon-(all|\d+)|raid-[45]-\d+)', detail_key)
            and detail_key in meta['details']):
        envelope = _load(path.parent / 'stats-details' / meta['generation'] / f'{detail_key}.json')
        if (isinstance(envelope, dict) and envelope.get('schema') == SCHEMA
                and envelope.get('generation') == meta['generation'] and envelope.get('scope') == meta['scope']
                and envelope.get('key') == detail_key and isinstance(envelope.get('data'), dict)):
            detail = envelope['data']
    if path.stem == 'dungeon':
        if detail_key == 'dungeon-all':
            payload['summary'] = detail
        else:
            payload['dungeons'] = [detail] if detail is not None else []
    elif path.stem == 'raid':
        # 仅替换选中难度、首领，其他卡片仍使用轻量概览。
        for item in payload.get('difficulties', []):
            for zone in item.get('zone_groups', []):
                zone['bosses'] = [detail if f"raid-{item['difficulty']}-{boss['boss_id']}" == detail_key else boss
                                  for boss in zone['bosses']]
                zone['bosses'] = [boss for boss in zone['bosses'] if boss is not None]
        payload['zone_groups'] = next((row['zone_groups'] for row in payload['difficulties'] if row['difficulty'] == 5), [])
    return payload
