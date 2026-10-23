"""Offline, fail-closed import of verified difficulty drops into the central catalog.

Rule artifacts must be resolved and explicitly bound to branch/instance/difficulty.
Tooltip evidence is the collector's verified request/response identity, not a rule
build assertion. No network, base-item update, Journal write, or batch publication.
"""
from copy import deepcopy
import hashlib
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ValidationError
from django.db import transaction

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import active_season
from botend.services.gear_builder_catalog_source import (_tooltip_details, _plain_text, STAT_NAMES_EN, STAT_NAMES_ZH)
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    default=str, allow_nan=False).encode()).hexdigest()


def _state(row):
    if row is None:
        return None
    value = {field.attname: deepcopy(getattr(row, field.attname))
             for field in row._meta.concrete_fields if field.name != 'variant_text_templates'}
    if isinstance(row, WowItemVariantSnapshot):
        value['effects_json'] = row.effects_data
    return value


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


# Official tooltips.js defines MAIN=1 and PTR2=10 (/ptr-2). Both endpoint
# identities were checked against native responses. PTR=2 and Beta remain closed:
# the generic branch label cannot silently substitute a different environment.
_VERIFIED_TOOLTIP_ENVS = {'retail': '1', 'ptr': '10'}
_SOURCE_FIELDS = ('build', 'table', 'filter', 'rows', 'total')


def _verified_rule(rule, item_id, context, build):
    records = {}
    for d in rule['evidence']:
        _require(all(k in d for k in (*_SOURCE_FIELDS, 'provider', 'url', 'source_sha256')), 'source envelope incomplete')
        _require(d['build'] == build and d['provider'] == 'wago', 'source provider/build mismatch')
        _require(isinstance(d['table'], str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', d['table']), 'invalid source table')
        f = d['filter']
        _require(isinstance(f, dict) and (len(f) == 1 or not f and d['table'] == 'ItemSquishEra'), 'invalid source filter')
        field, key = next(iter(f.items())) if f else (None, None)
        _require(field is None or isinstance(field, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', field)
                 and type(key) is int and (-32768 <= key <= 32767
                     if d['table'] == 'ItemBonusListLevelDelta' and field == 'ItemLevelDelta'
                     else key > 0), 'invalid source filter identity')
        _require(('field' not in d or d['field'] == field)
                 and ('key' not in d or (d['key'] is None if key is None else type(d['key']) is int and d['key'] == key)), 'source legacy identity mismatch')
        rows = d['rows']
        _require(isinstance(rows, list) and type(d['total']) is int and d['total'] == len(rows), 'incomplete source rows')
        _require(all(isinstance(r, dict) and type(r.get('ID')) is int and r['ID'] > 0
                     and (field is None or type(r.get(field)) is int and r[field] == key) for r in rows), 'invalid source row identity')
        _require(len({r['ID'] for r in rows}) == len(rows), 'duplicate source IDs')
        _require(isinstance(d['url'], str), 'source URL must be explicit string')
        try:
            u = urlsplit(d['url'])
            q = parse_qs(u.query, keep_blank_values=True)
            api_valid = (u.scheme == 'https' and u.netloc == 'wago.tools' and not u.fragment
                     and u.path == '/api/db2-find/' + d['table']
                     and field is not None and q.get('build') == [build] and q.get('filter[' + field + ']') == ['exact:' + str(key)]
                     and all(k in ('build', 'locale', 'filter[' + field + ']') and len(v) == 1 for k,v in q.items()))
            # Bulk CSV is the actual URL, never relabelled as an exact-find API.
            # The collector retains full source count/hash and filtered rows;
            # as with API envelopes, this is integrity, not an upstream signature.
            csv_valid = (u.scheme == 'https' and u.netloc == 'wago.tools' and not u.fragment
                         and u.path == '/db2/' + d['table'] + '/csv'
                         and q == {'build': [build], 'locale': ['enUS']}
                         and type(d.get('source_total')) is int and d['source_total'] >= d['total']
                         and isinstance(d.get('file_sha256'), str)
                         and re.fullmatch(r'[a-f0-9]{64}', d['file_sha256']) is not None)
            if field is None:
                csv_valid = csv_valid and d['source_total'] == d['total']
            valid = api_valid or csv_valid
            digest = hashlib.sha256(json.dumps({k:d[k] for k in _SOURCE_FIELDS},
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError):
            raise ValidationError('invalid source URL/hash')
        _require(valid and d['source_sha256'] == digest, 'source URL/hash mismatch')
        identity = d['table'], field, key
        _require(identity not in records or records[identity] == d, 'conflicting duplicate evidence identity')
        records[identity] = deepcopy(d)
    def source(table, field, key):
        d = records.get((table, field, key))
        _require(d is not None, 'missing evidence graph edge')
        return d
    try:
        resolved = resolve_difficulty_bonus_facts(item_id, context, build, tables=source)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise ValidationError('invalid evidence graph') from exc
    _require(resolved.get('status') == 'resolved' and not resolved.get('unresolved'), 'evidence graph unresolved')
    for field in ('item_level', 'quality', 'bonus_ids'):
        _require(resolved.get(field) == (sorted(rule[field]) if field == 'bonus_ids' else rule[field]),
                 'recomputed rule mismatch: ' + field)
    _require(resolved.get('initial_drop_kind') == rule.get('initial_drop_kind'),
             'recomputed initial drop kind mismatch')
    return resolved


def _integer_amount(raw):
    # en/zh Wowhead uses comma thousands, dot decimal; never erase decimal dots.
    _require(bool(re.fullmatch(r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)', raw)), 'non-integral/malformed raw attribute')
    value = int(raw.replace(',', ''))
    _require(value > 0, 'nonpositive raw attribute')
    return value


def _lossless_attributes(payload, details):
    raw = str(payload.get('tooltip') or '')
    region = re.search(r'<!--rf-->(.*?)<!--nameDescStats-->', raw, re.S | re.I)
    text = _plain_text(region.group(1) if region else raw)
    labels = {**{k.casefold():v for k,v in STAT_NAMES_ZH.items()}, **STAT_NAMES_EN}
    label_pattern = '|'.join(re.escape(k) for k in sorted(labels, key=len, reverse=True))
    stats, primary = {}, {}
    for amount, label in re.findall(rf'(?:^|\n)\s*([+\-]*[0-9][^\s]*)[ \t]*({label_pattern})(?![A-Za-z])', text, re.I):
        amount = amount[1:] if amount.startswith('+') else amount
        value = _integer_amount(amount)
        key = labels[label.casefold()]
        _require(key not in stats or stats[key] == value, 'conflicting repeated raw attribute')
        stats[key] = value
    for amount, names in re.findall(r'(?:^|\n)\s*([+\-]*[0-9][^\s]*)[ \t]*\[([^\]]*(?:Strength|Agility|Intellect|力量|敏捷|智力)[^\]]*)\]', text, re.I):
        value = _integer_amount(amount[1:] if amount.startswith('+') else amount)
        for label,key in labels.items():
            if key in ('strength','agility','intellect') and label in names.casefold():
                primary[key] = value
    _require(stats == details['stats'] and primary == details['primary_options'], 'lossy/unsupported raw attribute parse')
    weapon = bool(re.search(r'<!--(?:dmg|dps|spd)-->', raw, re.I))
    if weapon:
        school = r'(?:Physical|Holy|Fire|Nature|Frost|Shadow|Arcane|物理|神圣|火焰|自然|冰霜|暗影|奥术)'
        damage = re.search(rf'<!--dmg-->\s*([0-9][0-9,.]*)(?:\s*[-–]\s*([0-9][0-9,.]*))?\s*(?:{school}\s*)?(?:Damage|点?\s*(?:{school}\s*)?伤害)', raw, re.I)
        dps = re.search(r'<!--dps-->\s*[（(]\s*(?:([0-9][0-9,.]*)\s*(?:damage per second|每秒伤害)|每秒伤害\s*([0-9][0-9,.]*)点?[）)])', raw, re.I)
        speed = re.search(r'<!--spd-->\s*([0-9][0-9,.]*)', raw, re.I)
        _require(damage is not None and dps is not None and speed is not None, 'incomplete weapon attributes')
        low = _integer_amount(damage[1])
        high = _integer_amount(damage[2]) if damage[2] is not None else low
        _require(low <= high, 'invalid weapon damage range')
        stats.update(min_damage=low, max_damage=high)
        for key,match in (('weapon_dps',dps), ('weapon_speed',speed)):
            token = next(value for value in match.groups() if value is not None)
            _require(bool(re.fullmatch(r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?', token)),
                     'invalid weapon decimal')
            try:
                value = Decimal(token.replace(',', ''))
                number = float(value)
                _require(value > 0 and Decimal(str(number)) == value, 'lossy weapon decimal')
            except (InvalidOperation, OverflowError, ValueError):
                raise ValidationError('invalid weapon decimal')
            stats[key] = number
    return stats, primary


def import_difficulty_variant(*, rule, payload, tooltip_evidence, item_id,
                              data_branch, difficulty_id, item_context, instance_id,
                              rule_build, apply=False, expected_before_hash=None, sync_effects=True):
    """Validate and preview by default; explicit ``apply=True`` writes one variant.

    ``rule``: resolved artifact with item_id, build, data_branch, difficulty_id,
    item_context, instance_id, bonus_ids, item_level, quality and nonempty evidence.
    Evidence entries require build/provider='wago'/table/filter/rows/total/
    url/source_sha256. The hash covers build/table/filter/rows/total canonical
    JSON (sort_keys=True, separators=(',', ':'), ensure_ascii=True); complete
    envelopes are replayed through the real resolver, never trusted as results.
    Empty bonus IDs are accepted only when replay resolves initial_drop_kind
    'fixed_cosmetic'; its tooltip URL must omit bonus entirely (not bonus='').
    This checks offline integrity, not a cryptographic upstream signature.
    ``tooltip_evidence``: {identity_validated: True, item_id, url, data_branch,
    game_build: ''} supplied by a collector that verified the actual response ID.
    A raw payload ID, when present, must agree. Unversioned Wowhead must have an
    empty game_build; rule.build is never used as the attribute snapshot build.
    Reapply a dry-run's before_hash to reject intervening changes. Existing facts
    at the dedicated key are immutable: exact repeats are timestamp-preserving
    no-ops; conflicting evidence is refused, never silently overwritten.
    """
    _require(type(apply) is bool, 'apply must be explicit boolean')
    _require(isinstance(rule, dict) and isinstance(payload, dict)
             and isinstance(tooltip_evidence, dict), 'invalid artifact containers')
    for name, value in [('item_id', item_id), ('difficulty_id', difficulty_id),
                        ('item_context', item_context), ('instance_id', instance_id)]:
        _require(type(value) is int and value >= (0 if name == 'item_context' else 1),
                 'invalid ' + name)
        _require(type(rule.get(name)) is int and rule[name] == value, 'rule scope mismatch: ' + name)
    _require(data_branch in ('retail', 'ptr', 'beta') and rule.get('data_branch') == data_branch,
             'rule branch mismatch')
    _require(isinstance(rule_build, str) and bool(re.fullmatch(r'\d+\.\d+\.\d+\.\d+', rule_build))
             and rule.get('build') == rule_build, 'rule build mismatch')
    _require(rule.get('status') == 'resolved' and not rule.get('unresolved')
             and isinstance(rule.get('evidence'), list) and bool(rule['evidence'])
             and all(isinstance(row, dict) and row for row in rule['evidence']), 'unresolved rule/no evidence')
    level, quality, bonuses = rule.get('item_level'), rule.get('quality'), rule.get('bonus_ids')
    _require(type(level) is int and level > 0 and type(quality) is int and 0 <= quality <= 7,
             'invalid resolved level/quality')
    _require(isinstance(bonuses, list)
             and all(type(n) is int and n > 0 for n in bonuses)
             and len(set(bonuses)) == len(bonuses), 'invalid bonus IDs')
    bonuses = sorted(bonuses)
    verified = _verified_rule(rule, item_id, item_context, rule_build)
    _require(bool(bonuses) or verified.get('initial_drop_kind') in ('fixed_cosmetic', 'fixed_base'),
             'empty bonuses require recomputed fixed rule')
    evidence = deepcopy(tooltip_evidence)
    _require(evidence.get('identity_validated') is True
             and type(evidence.get('item_id')) is int and evidence['item_id'] == item_id
             and evidence.get('data_branch') == data_branch, 'tooltip identity/branch not verified')
    for key in ('id', 'itemId', 'item_id'):
        if key in payload:
            _require(type(payload[key]) is int and payload[key] == item_id, 'tampered tooltip ID')
    url = evidence.get('url')
    _require(isinstance(url, str), 'tooltip URL required')
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValidationError('malformed tooltip URL') from exc
    _require(parsed.scheme == 'https' and parsed.netloc == 'nether.wowhead.com'
             and not parsed.username and not parsed.password and not parsed.fragment
             and parsed.path == f'/tooltip/item/{item_id}', 'invalid tooltip URL identity')
    query = parse_qs(parsed.query, keep_blank_values=True)
    _require(data_branch in _VERIFIED_TOOLTIP_ENVS and query.get('dataEnv') == [_VERIFIED_TOOLTIP_ENVS.get(data_branch)], 'tooltip URL environment not verified')
    _require(all(key in ('bonus', 'dataEnv', 'locale') and len(values) == 1 for key,values in query.items()), 'unsupported/duplicate tooltip query')
    _require(not any(key.casefold() == 'ilvl' for key in query), 'forced ilvl forbidden')
    raw_bonus = query.get('bonus', [])
    if bonuses:
        _require(len(raw_bonus) == 1 and bool(re.fullmatch(r'\d+(?::\d+)*', raw_bonus[0])),
                 'URL bonus binding missing')
        url_bonuses = [int(n) for n in raw_bonus[0].split(':')]
    else:
        _require('bonus' not in query, 'fixed cosmetic URL must omit bonus parameter')
        url_bonuses = []
    _require(all(n > 0 for n in url_bonuses) and len(set(url_bonuses)) == len(url_bonuses) and set(url_bonuses) == set(bonuses), 'URL bonus mismatch')
    _require(type(payload.get('quality')) is int and payload['quality'] == quality,
             'raw tooltip quality mismatch')
    raw_level = re.findall(r'<!--ilvl-->\s*([0-9][0-9,.]*)', str(payload.get('tooltip', '')))
    _require(len(raw_level) == 1 and _integer_amount(raw_level[0]) == level, 'raw item level mismatch')
    details = _tooltip_details(payload)
    _require(details['item_level'] == level and details['quality'] == quality,
             'raw tooltip level/quality mismatch')
    if 'itemLevel' in payload:
        _require(type(payload['itemLevel']) is int and payload['itemLevel'] == level, 'response itemLevel mismatch')
    stats, primary = _lossless_attributes(payload, details)
    effects = list(details['effects'])
    # Table-cell boundaries are not line breaks in the shared extractor. Parse
    # native effect blocks independently so a preceding durability row cannot
    # swallow a real Use/Equip line. Keep the actual response and its hash intact.
    for block in re.findall(r'<!--itemEffects:\d+-->(.*?)<!--pvpEquip-->',
                            str(payload.get('tooltip') or ''), re.I | re.S):
        for effect in _tooltip_details({'tooltip': block})['effects']:
            if effect not in effects:
                effects.append(effect)
    _require(bool(stats or effects or primary), 'equipment attributes absent')
    game_build = evidence.get('game_build')
    _require(isinstance(game_build, str) and len(game_build) <= 64, 'attribute build must be explicit')
    _require(game_build == str(payload.get('game_build') or payload.get('build') or ''),
             'unproven attribute build')
    key = f'journal-difficulty:{data_branch}:{rule_build}:{instance_id}:{difficulty_id}:{item_context}:{level}:{_digest(bonuses)[:20]}'
    _require(len(key) <= 160, 'variant key too long')
    metadata = {
        'data_branch': data_branch,
        'difficulty_drop': dict(schema=1, difficulty_id=difficulty_id, item_context=item_context,
                                instance_id=instance_id, rule_build=rule_build, quality=quality,
                                provider='wago', bonus_ids=bonuses),
        'tooltip_source': dict(provider='wowhead', url=url, game_build=game_build,
                               data_env=query['dataEnv'][0],
                               response_sha256=_digest(payload)),
        'difficulty_rule_evidence': deepcopy(rule['evidence']),
        'primary_stat_values': primary,
    }
    if verified.get('initial_drop_kind') == 'fixed_cosmetic':
        metadata['difficulty_drop']['initial_drop_kind'] = 'fixed_cosmetic'
        metadata['difficulty_base_rule'] = deepcopy(verified['base_rule'])
    fields = dict(data_branch=data_branch, game_build=game_build, variant_type='drop_equipment',
                  item_level=level, bonus_ids=bonuses, stats_json=stats, effects_json=effects,
                  source_json=[], metadata=metadata)
    season = active_season()
    _require(season is not None and bool(season.gear_batch_key), 'no canonical active batch')
    item = WowItemSnapshot.objects.filter(item_id=item_id).first()
    _require(item is not None, 'base item must already exist')
    _require(item.catalog_type == 'equipment' and bool(item.slot_key or item.inventory_type), 'not equipment')
    if item.item_class_id == 2 or item.inventory_type in (13, 15, 17, 21, 22, 25, 26):
        _require(all(key in stats for key in ('min_damage', 'max_damage', 'weapon_dps', 'weapon_speed')), 'weapon source attributes absent')
    scope = dict(season_id=season.pk, batch_key=season.gear_batch_key, item_id=item.pk, variant_key=key)
    existing = WowItemVariantSnapshot.objects.filter(**scope).first()
    frozen = dict(season_id=season.pk, batch=season.gear_batch_key, item=_state(item), variant=_state(existing))
    before_hash = _digest(frozen)
    _require(expected_before_hash is None or expected_before_hash == before_hash, 'stale before hash')
    _require(existing is None or all((existing.effects_data if k == 'effects_json' else getattr(existing, k)) == v
                                    for k, v in fields.items()),
             'conflicting dedicated variant facts')
    result = dict(status='noop' if existing else 'dry_run', variant_key=key,
                  variant_id=existing.pk if existing else None, before_hash=before_hash,
                  season_id=season.pk, batch_key=season.gear_batch_key)
    if not apply:
        return result
    with transaction.atomic():
        locked_season = SeasonMeta.objects.select_for_update().get(pk=season.pk)
        locked_item = WowItemSnapshot.objects.select_for_update().get(pk=item.pk)
        locked_variant = WowItemVariantSnapshot.objects.select_for_update().filter(**scope).first()
        current = active_season()
        _require(current is not None and current.pk == season.pk and locked_season.is_active
                 and locked_season.gear_batch_key == season.gear_batch_key, 'active season/batch changed')
        _require(_digest(dict(season_id=season.pk, batch=locked_season.gear_batch_key,
                             item=_state(locked_item), variant=_state(locked_variant))) == before_hash,
                 'concurrent central facts changed')
        if locked_variant is None:
            locked_variant = WowItemVariantSnapshot.objects.create(**scope, **fields)
            result.update(status='created', variant_id=locked_variant.pk)
        else:
            result['status'] = 'noop'
        current = active_season()
        locked_season.refresh_from_db()
        _require(current is not None and current.pk == season.pk and locked_season.is_active
                 and locked_season.gear_batch_key == season.gear_batch_key, 'active season/batch changed')
        saved = WowItemVariantSnapshot.objects.get(pk=locked_variant.pk)
        _require(all((saved.effects_data if k == 'effects_json' else getattr(saved, k)) == v
                     for k, v in fields.items()), 'variant readback mismatch')
        locked_item.refresh_from_db()
        _require(_state(locked_item) == frozen['item'], 'base item changed')
        if sync_effects:
            from botend.services.wow_item_effects import sync_central_effects
            sync_central_effects(season=locked_season, item_ids=[item_id])
    return result
