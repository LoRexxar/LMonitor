"""Raid first-kill rule observations, independent of AI-generated M+ labels."""
from collections import defaultdict
from datetime import timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from django.utils import timezone

from botend.constants.wow import RAID_BOSS_CN, RAID_ZONE_CN
from botend.services.player_raiderio_activity import _date

DAY_ZONE = ZoneInfo('Asia/Shanghai')
TARGET_BOSSES = (6, 7, 8)


def same_day_first_kills(encounters, *, fetched_at):
    """Only distinct, raid-local 6/7/8 first kills can satisfy the rule."""
    if not fetched_at or timezone.is_naive(fetched_at):
        return []
    by_day, seen = defaultdict(list), set()
    for encounter in encounters:
        index = encounter.get('index')
        if type(index) is not int or index not in TARGET_BOSSES:
            continue
        if index in seen:
            return []
        seen.add(index)
        try:
            stamp = _date(encounter.get('firstDefeated'))
        except (ValueError, TypeError):
            continue
        if stamp is None or stamp > fetched_at:
            continue
        day = stamp.astimezone(DAY_ZONE).date().isoformat()
        by_day[day].append({**encounter, 'date': day})
    return next((sorted(rows, key=lambda item: item['index']) for day, rows in sorted(by_day.items())
                 if len(rows) >= 2), [])


def project_raid_observation(activity, season, identity, *, now=None):
    """Project one rule label from the independent central source, never AI/network."""
    result = {'code': 'raid_consumer', 'label': '团本消费', 'supported': False,
              'value': '证据不足', 'reason': '', 'evidence': [], 'note': ''}
    now = now or timezone.now()
    try:
        state = activity.payload_json.get('raid_first_kills', {})
        if (type(state.get('version')) is not int or state['version'] != 1
                or state.get('visible') is not True
                or state.get('status') not in ('ready', 'error', 'fetching')
                or state.get('identity_digest') != activity.identity_digest
                or state.get('rio_season') != season.rio_season):
            return result
        fetched = _date(state.get('fetched_at'))
        if not fetched or not timedelta(0) <= now - fetched <= timedelta(hours=48):
            return result
        source = state['source']
        character_id = source.get('character_id')
        if type(character_id) is not int or character_id <= 0:
            return result
        scored_id = (activity.scored_runs_json or {}).get('ui', {}).get('characterId')
        if scored_id is not None and scored_id != character_id:
            return result
        central = {row['id']: row for row in season.raid_encounters}
        raids = state['raids']
        if not isinstance(raids, list) or len(raids) > 20:
            return result
        seen_raids = set()
        matches = []
        for raid in raids:
            slug = raid['slug']
            if not isinstance(slug, str) or not slug or slug in seen_raids:
                return result
            seen_raids.add(slug)
            rows = raid['encounters']
            if not isinstance(rows, list) or len(rows) != 3:
                return result
            if any(type(row.get('index')) is not int for row in rows):
                return result
            if {row['index'] for row in rows} != set(TARGET_BOSSES):
                return result
            ids = [row['wowEncounterId'] for row in rows]
            if (any(type(value) is not int or value not in central for value in ids)
                    or len(set(ids)) != len(ids)
                    or len({row['slug'] for row in rows}) != len(rows)):
                return result
            for row in rows:
                if row.get('firstDefeated') is not None:
                    if type(row.get('numKills')) is not int or row['numKills'] <= 0:
                        return result
                    stamp = _date(row['firstDefeated'])
                    if stamp is None or stamp > fetched:
                        return result
            matched = same_day_first_kills(rows, fetched_at=fetched)
            if matched:
                matches.append((slug, raid, matched))
        if not matches:
            return result
        _, raid, matched = sorted(matches, key=lambda item: item[0])[0]
        profile = 'https://raider.io/characters/' + '/'.join(quote(identity[key], safe='')
            for key in ('region', 'realm_slug', 'character_name'))
        numbers = '、'.join(str(row['index']) for row in matched)
        raid_name = RAID_ZONE_CN.get(raid['name'], raid['name'])
        evidence = []
        for row in matched:
            name = central[row['wowEncounterId']].get('name') or row['slug']
            name = RAID_BOSS_CN.get(name, name)
            first = _date(row['firstDefeated']).astimezone(DAY_ZONE).strftime('%Y-%m-%d %H:%M:%S')
            evidence.append({'text': f"{raid_name} · {row['index']}号 {name}：史诗首次击杀 {first}（北京时间）。",
                             'source_url': profile})
        result.update(supported=True, value='团本消费选手',
            reason=f"史诗{numbers}号于 {matched[0]['date']} 同日首杀（北京时间）。",
            evidence=evidence, note='日期规则标记，不代表已确认付费。')
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        # Broken/obsolete cached source is unknown, never a public conclusion.
        pass
    return result
