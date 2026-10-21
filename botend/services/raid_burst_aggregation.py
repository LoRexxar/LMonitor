"""每日团本聚合的战斗时间轴：原始事件缓存、后台分组、私有快照发布。"""

from django.utils import timezone
from utils.log import logger

from botend.services.raid_burst_events import RAW_VERSION, fetch_events, realm_key
from botend.services.raid_burst_skills import SUPPORTED, CATALOG_BUILD
from botend.services.raid_burst_snapshot import (
    SCHEMA, digest, private_root, read_blob, read_json, read_manifest, write_json,
)


ANALYSIS_VERSION = 1
SAMPLE_LIMIT = 10
SELECTION_POLICY = 'top-usable-unique-player-no-pi-v1'
LUST = {2825, 32182, 80353, 390386, 264667, 272678}
FIELDS = ('report_code', 'fight_id', 'character_name', 'realm', 'region', 'class_name',
          'spec_name', 'boss_id', 'difficulty', 'dps')


def normalize(raw, row, rank, config):
    if raw.get('invalid'):
        return None, 'invalid'
    fight = next(f for f in raw['report']['fights'] if f['id'] == row['fight_id'])
    actor = raw['actor']
    start, end = fight['startTime'], fight['endTime']
    streams = raw['streams']
    if not all(streams.get(name, {}).get('complete') for name in ('casts', 'buffs', 'combatants')):
        raise ValueError('禁止用未完成的事件流聚合')
    info = [e for e in streams['combatants']['data']
            if e.get('sourceID') == actor['id'] and e.get('fight') == fight['id']
            and e.get('specID') == config['spec_id']]
    if len(info) != 1 or end <= start:
        return None, 'invalid'
    buffs = [e for e in streams['buffs']['data'] if not e.get('fake')
             and e.get('targetID') == actor['id'] and e.get('fight') == fight['id']
             and start <= e.get('timestamp', -1) <= end]
    initial = {a.get('ability') for a in info[0].get('auras', [])}
    if 10060 in initial or any(e.get('abilityGameID') == 10060 for e in buffs):
        return None, 'pi'
    windows = []
    for ability in LUST:
        opened = start if ability in initial else None
        for event in sorted(buffs, key=lambda e: e['timestamp']):
            if event.get('abilityGameID') != ability:
                continue
            if event.get('type') == 'applybuff' and opened is None:
                opened = event['timestamp']
            elif event.get('type') == 'removebuff' and opened is not None:
                windows.append({'start': round((opened - start) / 1000, 3),
                                'end': round((event['timestamp'] - start) / 1000, 3)})
                opened = None
        if opened is not None:
            windows.append({'start': round((opened - start) / 1000, 3),
                            'end': round((end - start) / 1000, 3)})
    merged = []
    for window in sorted(windows, key=lambda w: w['start']):
        if merged and window['start'] <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], window['end'])
        else:
            merged.append(window)
    if not merged:
        return None, 'no_lust'
    lust = merged[0]['start']
    phases = sorted([{'id': p['id'], 'start': round((p['startTime'] - start) / 1000, 3)}
                     for p in fight.get('phaseTransitions') or []], key=lambda p: p['start'])
    phase = next((p['id'] for p in reversed(phases) if p['start'] <= lust), None)
    group = 'opener' if lust <= 10 else f'phase{phase}' if phase is not None else 'midfight'
    label = ('起手嗜血（开怪10秒内）' if lust <= 10 else
             f'中途嗜血 · 第{phase}阶段' if phase is not None else '中途嗜血 · 阶段未知')
    casts = [e for e in streams['casts']['data'] if e.get('type') == 'cast' and not e.get('fake')
             and e.get('sourceID') == actor['id'] and e.get('fight') == fight['id']
             and start <= e.get('timestamp', -1) <= end]
    aliases = config.get('cast_aliases', {})
    times = {spell: sorted({round((e['timestamp'] - start) / 1000, 3) for e in casts
                            if aliases.get(str(e.get('abilityGameID')), str(e.get('abilityGameID'))) == spell})
             for spell in config['spells']}
    return {
        'rank': rank, 'player': {'name': actor['name'], 'realm': actor.get('server') or row['realm'],
                                 'region': row.get('region') or ''},
        'dps': row['dps'], 'duration': round((end - start) / 1000, 3),
        'url': f"https://www.warcraftlogs.com/reports/{row['report_code']}?fight={fight['id']}&type=casts&source={actor['id']}",
        'incoming_complete': True, 'pi_observed': False, 'times': times, 'phases': phases,
        'lust_start': lust, 'lust_windows': merged, 'lust_group': group, 'lust_label': label,
        'lust_focus': {spell: [round(t - lust, 3) for t in sequence if -20 <= t - lust <= 40]
                       for spell, sequence in times.items()},
    }, None


def build_groups(samples, spells):
    """合并总宽最多5秒；前台只投影成员，不重新合并或拼出虚构时间轴。"""
    result = {}
    for key in dict.fromkeys(s['lust_group'] for s in samples):
        members = [s for s in samples if s['lust_group'] == key]
        result[key] = {}
        for spell in spells:
            windows, plans = [], []
            points = sorted((t, row['rank']) for row in members for t in row['lust_focus'][spell])
            for time, rank in points:
                if not windows or time - windows[-1]['start'] > 5:
                    windows.append({'start': time, 'end': time, 'ranks': []})
                window = windows[-1]
                window['end'] = time
                if rank not in window['ranks']:
                    window['ranks'].append(rank)
            for row in members:
                sequence = row['times'][spell]
                plan = next((p for p in plans if len(p['ranges']) == len(sequence)
                             and all(max(w['end'], t) - min(w['start'], t) <= 5
                                     for w, t in zip(p['ranges'], sequence))), None)
                if plan is None:
                    plan = {'ranks': [], 'ranges': [{'start': t, 'end': t} for t in sequence]}
                    plans.append(plan)
                plan['ranks'].append(row['rank'])
                for window, time in zip(plan['ranges'], sequence):
                    window.update(start=min(window['start'], time), end=max(window['end'], time))
            result[key][spell] = {'windows': windows, 'plans': plans}
    return result


def player_key(row):
    """Only reserve an identity after a valid sample; failed logs may retry."""
    fields = [row.get(k) for k in ('region', 'realm', 'character_name')]
    if not all(isinstance(value, str) and value.strip() for value in fields):
        return None
    region, realm, name = fields
    realm = realm_key(realm)
    return (region.strip().casefold(), realm, name.strip().casefold()) if realm else None


def build_scope(rows, scope, config, root, guard, client):
    # 身份稳定排序后再按 DPS 排名；数据库换主键、刷新时间不导致重抓或重算。
    rows = sorted([{field: row.get(field) for field in FIELDS} for row in rows],
                  key=lambda r: (-r['dps'], digest(r)))
    rows = list({digest({k: r[k] for k in FIELDS if k != 'dps'}): r
                 for r in reversed(rows)}.values())
    rows.sort(key=lambda r: (-r['dps'], digest(r)))
    fingerprint = digest({'version': ANALYSIS_VERSION, 'raw_version': RAW_VERSION,
                          'selection_policy': SELECTION_POLICY, 'sample_limit': SAMPLE_LIMIT,
                          'scope': scope, 'config': config, 'rows': rows})
    directory = root / str(scope['season_id']) / scope['class_name'] / scope['spec_name']
    prepared_path = directory / 'prepared' / f'{fingerprint}.json'
    prepared = read_json(prepared_path)
    if isinstance(prepared, dict) and prepared.get('key') == scope['key']:
        published = read_blob(directory, prepared.get('blob'))
        if published and published.get('input_fingerprint') == fingerprint:
            logger.info('[RaidBurst] 复用已完成快照 %s', scope['key'])
            return prepared
    old = read_manifest(directory) or read_manifest(directory, 'previous.json')
    previous = next((s for s in old['scopes'] if s['key'] == scope['key']), None) if old else None
    if previous:
        published = read_blob(directory, previous['blob'])
        if published and published['input_fingerprint'] == fingerprint:
            logger.info('[RaidBurst] 复用已发布快照 %s', scope['key'])
            return previous
    logger.info('[RaidBurst] 开始生成 %s，日志数=%s', scope['key'], len(rows))
    samples, counts = [], dict(pi=0, no_lust=0, invalid=0, duplicate=0)
    selected, attempted = set(), 0
    for rank, row in enumerate(rows, 1):
        if len(samples) >= SAMPLE_LIMIT:
            break
        identity = player_key(row)
        if identity is not None and identity in selected:
            counts['duplicate'] += 1
            continue
        # Counts checked candidates, including cache hits and missing identity,
        # never unvisited tails or duplicates skipped before fetching.
        attempted += 1
        if identity is None:
            counts['invalid'] += 1
            continue
        raw = fetch_events(root, row, client, guard)
        sample, reason = normalize(raw, row, rank, config)
        if reason:
            counts[reason] += 1
        else:
            samples.append(sample)
            selected.add(identity)
    payload = {
        'schema': SCHEMA, 'input_fingerprint': fingerprint, 'scope': scope,
        'generated_at': timezone.now().isoformat(), 'skill_names': config['spells'],
        'skill_role': config.get('role', '输出'), 'catalog_build': CATALOG_BUILD,
        'selection_policy': SELECTION_POLICY, 'selection_limit': SAMPLE_LIMIT,
        'candidate_count': len(rows), 'attempted': attempted,
        'successful': attempted - counts['invalid'],
        'excluded_duplicate': counts['duplicate'],
        'excluded_pi': counts['pi'], 'excluded_no_lust': counts['no_lust'],
        'excluded_invalid': counts['invalid'], 'samples': samples,
        'groups': build_groups(samples, config['spells']),
    }
    signature = digest(payload)
    with guard():
        write_json(directory / 'snapshots' / f'{signature}.json', payload)
        descriptor = dict(scope, blob=signature, sample_count=len(samples))
        write_json(prepared_path, descriptor)
    logger.info('[RaidBurst] 完成 %s，有效=%s，灌注=%s，无嗜血=%s，无效=%s',
                scope['key'], len(samples), counts['pi'], counts['no_lust'], counts['invalid'])
    return descriptor


def aggregate_raid_burst(season, class_name, spec_name, *, guard, source_cutoff=None):
    """仅由团本发布入口调用；全部范围完成后切换目录，失败保留上次结果。"""
    config = SUPPORTED.get((class_name, spec_name))
    if not config:
        return None
    from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
    from botend.models import SpecRaidRanking
    from botend.constants.wow import RAID_BOSS_CN

    root, client = private_root(), SpecDetailBase(None, None)
    encounters = {int(e['id']): e for e in season.raid_encounters}
    for zone in season.raid_zones or []:
        encounters.update({int(e['id']): e for e in zone.get('encounters', [])})
    scopes = []
    for boss_id, encounter in sorted(encounters.items()):
        for difficulty in (5, 4):
            scope = {'season_id': season.id, 'class_name': class_name, 'spec_name': spec_name,
                     'boss_id': boss_id, 'difficulty': difficulty,
                     'key': f'{season.id}/{class_name}/{spec_name}/{difficulty}/{boss_id}',
                     'label': f"{'史诗' if difficulty == 5 else '英雄'} · {RAID_BOSS_CN.get(encounter['name'], encounter['name'])} · {config['label']} · 赛季{season.id}"}
            with guard():
                rows = SpecRaidRanking.objects.filter(
                    season_id=season.id, class_name=class_name, spec_name=spec_name,
                    boss_id=boss_id, difficulty=difficulty)
                if source_cutoff is not None:
                    from django.db.models import Q
                    from botend.services.wcl_collection_cycle import CycleOwnershipLost
                    if rows.filter(last_updated__gt=source_cutoff).exists():
                        raise CycleOwnershipLost('raid scope facts newer than collected cutoff')
                    rows = rows.filter(Q(last_updated__lte=source_cutoff) | Q(last_updated__isnull=True))
                rows = list(rows.values(*FIELDS))
            scopes.append(build_scope(rows, scope, config, root, guard, client))
    directory = root / str(season.id) / class_name / spec_name
    manifest = {'schema': SCHEMA, 'scopes': scopes,
                'source_cutoff': source_cutoff.isoformat() if hasattr(source_cutoff, 'isoformat') else source_cutoff,
                'profile_digest': digest({'analysis': ANALYSIS_VERSION, 'raw': RAW_VERSION, 'config': config,
                                          'selection_policy': SELECTION_POLICY, 'sample_limit': SAMPLE_LIMIT})}
    with guard():
        previous = read_manifest(directory)
        if previous != manifest:
            if previous:
                write_json(directory / 'previous.json', {'data': previous, 'digest': digest(previous)})
            write_json(directory / 'index.json', {'data': manifest, 'digest': digest(manifest)})
    return manifest
