"""Bounded current-Wowhead derived evidence; never rewrites 69594 frozen facts.

Collector schema: {schema:1, source_build:'12.1.5.70077', records:[record,...]}.
Exactly 16 records, each with variant_id, before (captured frozen fields plus
updated_at), evidence (the wow_item_difficulty normalized envelopes), and
 tooltip:{url,payload,response_sha256}. adapt_record validates and converts this
into the internal schema with baseline_sha256, rule_build, added_bonus_ids,
envelopes:[{url,data:<complete Wago envelope>}], url, response, response_sha256.
Command supplies evidence_ref and evidence_sha256 of the single artifact file.
Digests are canonical decoded-JSON hashes, not HTTP byte hashes/signatures.
Only direct, unconditional ItemXBonusTree -> node -> Type23 -> effect is supported.
"""
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import urlsplit, parse_qs
from django.core.exceptions import ValidationError
from .gear_builder_catalog_source import _tooltip_details
from .wow_item_difficulty_import import _lossless_attributes

ITEMS = {281235, 281236, 281238, 281239}
DIFFICULTIES = {14, 15, 17, 233}
BUILD = '12.1.5.70077'


def require(ok, message):
    if not ok:
        raise ValidationError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def baseline_state(variant):
    # 中央特效引用是可重建的展示字段，不属于旧证据绑定的冻结事实。
    # 排除它也保持迁移前已保存的证据摘要有效。
    state = {f.attname: deepcopy(getattr(variant, f.attname))
             for f in variant._meta.concrete_fields if f.name not in ('created_at', 'updated_at', 'effect_values')}
    state['effects_json'] = variant.effects_data
    state['metadata'].pop('effect_supplement', None)
    return state


def baseline_sha256(variant):
    state = baseline_state(variant)
    metadata = state.pop('metadata')
    for key in ('difficulty_drop', 'tooltip_source', 'primary_stat_values'):
        state[key] = metadata.get(key)
    return digest(state)


def adapt_record(variant, record, source_build):
    """Adapt collector schema, verifying its captured before-state and DB2 hashes."""
    require(source_build == BUILD, 'collector source build mismatch')
    before = record['before']
    required = ('item_id','season_id','batch_key','variant_key','data_branch','item_level',
                'game_build','bonus_ids','stats_json','effects_json','metadata','source_json','updated_at')
    for key in required:
        expected = deepcopy(before[key])
        actual = getattr(variant, key)
        if key == 'effects_json':
            actual = variant.effects_data
        if key == 'item_id':
            actual = variant.item.item_id
        elif key == 'updated_at':
            # A previously installed identical supplement legitimately changed this timestamp.
            if variant.metadata.get('effect_supplement'):
                continue
            actual = actual.isoformat()
        elif key == 'metadata':
            actual = deepcopy(actual)
            actual.pop('effect_supplement', None)
            expected.pop('effect_supplement', None)
        require(digest(actual) == digest(expected), 'captured before mismatch: '+key)
    envelopes = []
    for e in record['evidence']:
        body = dict(build=e['build'], table=e['table'], filter={e['field']:e['key']},
                    total=e['total'], rows=e['rows'])
        sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        require(sha == e['source_sha256'] and e['build'] == BUILD, 'DB2 source digest mismatch')
        envelopes.append(dict(url=e['url'], data=dict(data=e['rows'], total=e['total'],
                               current_page=1, last_page=1, next_page_url=None)))
    tooltip = record['tooltip']
    bonuses = parse_qs(urlsplit(tooltip['url']).query)['bonus'][0].split(':')
    added = [int(x) for x in bonuses if int(x) not in variant.bonus_ids]
    return dict(record, schema=1, baseline_sha256=baseline_sha256(variant), rule_build=BUILD,
                added_bonus_ids=added, envelopes=envelopes, url=tooltip['url'],
                response=tooltip['payload'], response_sha256=tooltip['response_sha256'])


def _rows(envelopes, table, field, value):
    matches = []
    for entry in envelopes:
        u = urlsplit(entry['url'])
        q = parse_qs(u.query)
        if u.path != '/api/db2-find/' + table or q.get('filter['+field+']') != ['exact:'+str(value)]:
            continue
        require(u.scheme == 'https' and u.netloc == 'wago.tools' and not u.fragment, 'invalid DB2 source')
        require(q.get('build') == [BUILD] and q.get('locale') == ['enUS'], 'wrong DB2 build/locale')
        require(set(q) <= {'build','locale','filter['+field+']','page'} and q.get('page', ['1']) == ['1'], 'filtered/partial envelope')
        env = entry['data']; rows = env['data']
        require(type(env['total']) is int and env['total'] == len(rows) and
                env['current_page'] == 1 and env['last_page'] == 1 and
                env.get('next_page_url') is None, 'incomplete DB2 envelope')
        require(len({r['ID'] for r in rows}) == len(rows), 'duplicate DB2 rows')
        require(all(r.get(field) == value for r in rows), 'wrong DB2 row identity')
        matches.append(rows)
    require(len(matches) == 1, 'missing/duplicate DB2 envelope: '+table)
    return matches[0]


def validated_supplement(variant, record):
    """Pure validation. Raises ValidationError; returns only the compact projection."""
    try:
        return _validated(variant, record)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationError('malformed supplement evidence') from exc


def _validated(v, r):
    meta = v.metadata; drop = meta['difficulty_drop']
    require(v.item.item_id in ITEMS and v.data_branch == 'ptr' and
            v.variant_type == 'drop_equipment' and v.variant_key.startswith('journal-difficulty:ptr:12.1.5.69594:1324:') and
            drop['instance_id'] == 1324 and drop['difficulty_id'] in DIFFICULTIES and
            drop['rule_build'] == '12.1.5.69594', 'outside bounded scope')
    require(v.effects_data == [], 'original effects must be empty')
    require(r['schema'] == 1 and r['variant_id'] == v.pk and
            r['baseline_sha256'] == baseline_sha256(v), 'baseline/identity mismatch')
    require(r['rule_build'] == BUILD, 'unsupported supplement rule build')
    require(isinstance(r['evidence_ref'], str) and bool(r['evidence_ref']) and
            re.fullmatch('[0-9a-f]{64}', r['evidence_sha256']), 'missing audit reference')
    added = r['added_bonus_ids']
    require(isinstance(added, list) and 0 < len(added) <= 4 and
            all(type(x) is int and x > 0 for x in added) and len(set(added)) == len(added) and
            not set(added).intersection(v.bonus_ids), 'invalid additional bonuses')
    envs = r['envelopes']
    require(isinstance(envs, list) and len(envs) <= 32, 'evidence budget exceeded')
    links = _rows(envs, 'ItemXBonusTree', 'ItemID', v.item.item_id)
    proven = set()
    gates = ('ItemContext','ChildItemBonusTreeID','ChildItemLevelSelectorID',
             'ChildItemBonusListGroupID','IblGroupPointsModSetID','MinMythicPlusLevel',
             'MaxMythicPlusLevel','ItemCreationContextGroupID','Flags')
    # Only claimed effect-only trees need envelopes, not unrelated level trees.
    for link in links:
        tree = link['ItemBonusTreeID']
        candidates = [e for e in envs if urlsplit(e['url']).path == '/api/db2-find/ItemBonusTreeNode'
                      and parse_qs(urlsplit(e['url']).query).get('filter[ParentItemBonusTreeID]') == ['exact:'+str(tree)]]
        if not candidates:
            continue
        for node in _rows(envs, 'ItemBonusTreeNode', 'ParentItemBonusTreeID', tree):
            if node['ChildItemBonusListID'] in added:
                from .wow_item_difficulty_tree import tree_applies
                applicability = tree_applies(tree, v.item.item_id, drop['item_context'],
                    read_table=lambda table, field, value: _rows(envs, table, field, value))
                require(applicability['applies'], 'effect tree does not apply to item')
                require(all(type(node[k]) is int and node[k] == 0 for k in gates), 'conditional effect node')
                proven.add(node['ChildItemBonusListID'])
    require(proven == set(added), 'no complete unconditional item/bonus chain')
    effects_by_spell = {}
    for bonus in added:
        rows = _rows(envs, 'ItemBonus', 'ParentItemBonusListID', bonus)
        require(bool(rows), 'empty effect bonus')
        for row in rows:
            require(row['Type'] == 23 and all(row[k] == 0 for k in ('Value_1','Value_2','Value_3')), 'non-Type23 bonus')
            effects = _rows(envs, 'ItemEffect', 'ID', row['Value_0'])
            require(len(effects) == 1, 'missing effect')
            effect = effects[0]
            require(effect['ChrSpecializationID'] == 0 and effect['PlayerConditionID'] == 0 and
                    effect['TriggerType'] == 1, 'conditional/non-equip effect')
            require(effect['SpellID'] not in effects_by_spell, 'duplicate spell effect')
            effects_by_spell[effect['SpellID']] = effect
    u = urlsplit(r['url']); q = parse_qs(u.query)
    require(u.scheme == 'https' and u.netloc == 'nether.wowhead.com' and not u.fragment and
            u.path == '/tooltip/item/'+str(v.item.item_id) and set(q) == {'dataEnv','locale','bonus'} and
            q['dataEnv'] == ['10'] and q['locale'] == ['enUS'], 'wrong tooltip identity/environment')
    require(len(q['bonus']) == 1 and re.fullmatch(r'[1-9]\d*(?::[1-9]\d*)*', q['bonus'][0]), 'invalid bonus URL')
    bonuses = [int(x) for x in q['bonus'][0].split(':')]
    require(sorted(bonuses) == sorted(v.bonus_ids + added), 'wrong tooltip bonus set')
    payload = r['response']
    require(digest(payload) == r['response_sha256'], 'response hash mismatch')
    details = _tooltip_details(payload)
    require(details['item_level'] == v.item_level and details['quality'] == drop['quality'], 'level/quality changed')
    stats, primary = _lossless_attributes(payload, details)
    require(stats == v.stats_json and primary == meta.get('primary_stat_values', {}), 'attributes changed')
    blocks = re.findall(r'<!--useText:[^>]*-->(.*?)<!--useText:[^>]*-->', payload['tooltip'], re.S | re.I)
    require(len(blocks) == len(details['effects']) == len(effects_by_spell) > 0, 'effect block count mismatch')
    result = []
    for block, parsed in zip(blocks, details['effects']):
        anchors = re.findall(r'<a\b[^>]*href=[\'"][^\'"]*?/spell=(\d+)[^\'"]*[\'"]', block, re.I)
        require(len(anchors) == 1 and int(anchors[0]) in effects_by_spell, 'unbound effect anchor')
        effect = effects_by_spell.pop(int(anchors[0]))
        require(_tooltip_details({'tooltip':'<!--useText:0-->'+block+'<!--useText:1-->'})['effects'] == [parsed], 'effect text association mismatch')
        result.append(dict(parsed, spell_id=effect['SpellID'], item_effect_id=effect['ID'], trigger_type=effect['TriggerType']))
    return dict(schema=1, baseline_sha256=r['baseline_sha256'], effects=result,
                source=dict(rule_build=BUILD, url=r['url'], response_sha256=r['response_sha256'],
                            bonus_ids=bonuses, kind='current-wowhead-derived-supplement'),
                evidence_ref=r['evidence_ref'], evidence_sha256=r['evidence_sha256'])


def supplemental_effects(variant):
    """Fail closed when any frozen fact changed; never mutate the source list."""
    supplement = (variant.metadata or {}).get('effect_supplement')
    if (variant.effects_data == [] and isinstance(supplement, dict) and
            supplement.get('schema') == 1 and supplement.get('baseline_sha256') == baseline_sha256(variant)
            and isinstance(supplement.get('effects'), list)):
        return deepcopy(supplement['effects'])
    return deepcopy(variant.effects_data)
