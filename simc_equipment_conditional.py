"""Opt-in conditional amplitude protocol. No database, item allowlist or simulator.

Authorization is supplied by the trusted freezer/executor, NEVER from input text.
Hash pinning is integrity, not a signature: callers must not accept client pins.
"""
import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def input_digest(text):
    from simc_equipment_control import NATIVE_PROOF_MARKER
    return hashlib.sha256(('\n'.join(l for l in text.splitlines() if not l.startswith(NATIVE_PROOF_MARKER)) + '\n').encode()).hexdigest()


def validate_contract(policy, expectation, *, authorization=None, require_authorization=True):
    from simc_equipment_control import validate_effect_policy, validate_equipment_expectation
    try:
        if (set(policy) != {'version','comparison_kind','target_slots','changed_slots','context_slots','rules'}
                or policy['version'] != 3 or policy['comparison_kind'] != 'conditional_increment'):
            raise ValueError('conditional policy')
        changed, context = policy['changed_slots'], policy['context_slots']
        if (len(changed) != 1 or len(context) != 1 or set(changed) & set(context)
                or len(policy['target_slots']) != 2 or set(policy['target_slots']) != set(changed + context)):
            raise ValueError('conditional roles')
        validate_effect_policy({'candidate_type':'gear_swap', 'gear_swaps':[{'slot':s} for s in policy['target_slots']],
            'equipment_effect_policy':{'version':2,'target_slots':policy['target_slots'],'rules':policy['rules']}})
        if set(expectation) != {'schema_version','targets','relations','identity'} or expectation['schema_version'] != 2:
            raise ValueError('conditional expectation')
        validate_equipment_expectation({'schema_version':1,'targets':expectation['targets']},policy['target_slots'])
        targets = {t['slot']:t for t in expectation['targets']}
        if set(targets) != set(policy['target_slots']) or len(expectation['relations']) != 1:
            raise ValueError('conditional targets')
        relation = expectation['relations'][0]
        if relation.get('schema_version') != 1 or relation['relation_hash'] != digest({k:v for k,v in relation.items() if k != 'relation_hash'}):
            raise ValueError('conditional relation integrity')
        identity = expectation['identity']
        if (set(identity) != {'binary_sha256','revision','dbc_build'}
                or not re.fullmatch('[0-9a-f]{64}',identity['binary_sha256'])
                or not re.fullmatch('[0-9a-f]{40}',identity['revision'])
                or relation['game_build'] != identity['dbc_build']):
            raise ValueError('conditional identity')
        if require_authorization and (not authorization or authorization['identity'] != identity
                or relation['relation_hash'] not in authorization['relation_hashes']):
            raise ValueError('conditional authorization missing/mismatch')
        provenance, source = relation['provenance'], relation['source_fact']
        if (any(provenance[k] != identity[k] for k in identity)
                or source['revision'] != identity['revision'] or source['source_clean_before'] is not True
                or source['fact_hash'] != provenance['source_fact_hash']
                or source['fact_hash'] != digest({k:v for k,v in source.items() if k != 'fact_hash'})
                or source['kind'] != 'independent_reviewed_upstream_source_relation'
                or not re.fullmatch('[0-9a-f]{64}',source['source_file']['sha256'])
                or len(source['snippets']) < 3
                or any(not re.fullmatch('[0-9a-f]{64}',s['snippet_sha256']) for s in source['snippets'])
                or any(r['build'] != identity['dbc_build'] for r in source['dbc_carrier_chain'])):
            raise ValueError('conditional source provenance')
        for key in ('modifier_driver_spell_id','consumer_driver_spell_id','operation','metric'):
            if source[key] != relation[key]: raise ValueError('conditional source relation mismatch')
        if (relation['operation'] != 'multiply' or relation['metric'] != 'periodic_damage_amount'
                or relation['factor'] != source['factor_when_present'] / source['factor_when_absent']
                or not math.isfinite(relation['factor']) or relation['factor'] <= 0
                or type(relation['relative_tolerance']) not in (int,float)
                or not 0 <= relation['relative_tolerance'] <= 1e-9
                or len(relation['consumer_event_spell_ids']) != 1):
            raise ValueError('unsupported conditional witness')
        for slot,role,kind,driver,condition in ((changed[0],'changed','dependent_modifier','modifier_driver_spell_id','changed_modifier'),
                (context[0],'context','direct','consumer_driver_spell_id','fixed_consumer')):
            target = targets[slot]
            conditions = [c for c in relation['conditions'] if c['kind'] == condition]
            if (target.get('role') != role or target.get('effect_kind') != kind
                    or target['game_build'] != identity['dbc_build'] or target['driver_spell_ids'] != [relation[driver]]
                    or len(conditions) != 1 or any(conditions[0][k] != target[k] for k in ('slot','item_id','required_bonus_ids'))
                    or target['event_spell_ids'] != (relation['consumer_event_spell_ids'] if role == 'context' else [])):
                raise ValueError('conditional role/relation binding')
        return relation
    except (KeyError,TypeError,IndexError,AttributeError,ZeroDivisionError) as exc:
        raise ValueError('malformed conditional contract') from exc


def passive_effects(export, target, relation):
    """Unknown is authorized only for this pinned modifier's native item block."""
    from simc_equipment_control import _options
    if target['role'] != 'changed': return export['effects']
    driver = relation['modifier_driver_spell_id']
    if _options(export['profile_value']).get('id') != str(target['item_id']):
        raise ValueError('passive item identity')
    blocks = [dict(re.findall(r'\b(\w+)=([^\s]+)', b)) for b in re.findall(r'\beffect=\{\s*([^{}]*?)\s*\}',export['record'])]
    matches = [b for b in blocks if b.get('source')=='item' and b.get('type')=='unknown' and b.get('driver')==str(driver)]
    if len(matches) != 1 or len([b for b in blocks if b.get('source')=='item']) != 1:
        raise ValueError('passive native source missing/ambiguous')
    return [{'source':'item','type':'unknown','driver':driver,'trigger':None,'origin':None}]


def authorize_exports(exports, expectation, relation):
    from simc_equipment_control import _require_declared_effects
    for target in expectation['targets']:
        if target['role']=='changed':
            exports[target['slot']]['effects'] = passive_effects(exports[target['slot']],target,relation)
        else:
            effects = exports[target['slot']]['effects']
            if not any(e.get('origin',{} ) and e['origin']['driver']==relation['consumer_driver_spell_id']
                and e['driver']==relation['consumer_runtime_driver_spell_id'] for e in effects):
                raise ValueError('consumer native origin missing')
    _require_declared_effects(exports,expectation)


def bind_binary(binary, expectation):
    with Path(binary).open('rb') as stream:
        actual = hashlib.file_digest(stream,'sha256').hexdigest()
    if actual != expectation['identity']['binary_sha256']: raise ValueError('conditional binary mismatch')


def validate_event_samples(numeric):
    """One event's per-iteration aggregates share a discrete sample cohort.

    Sums remain continuous. Samples need not equal options.iterations: SimC can
    omit warm-up iterations, and the validator must not guess that adjustment.
    """
    counts = []
    for key in ('num_executes', 'num_ticks', 'num_tick_results', 'actual_amount'):
        total, count = numeric[key]['sum'], numeric[key]['count']
        if type(total) not in (int, float) or not math.isfinite(total) or total <= 0:
            raise ValueError('conditional consumer not effective')
        if (type(count) not in (int, float) or not math.isfinite(count)
                or count <= 0 or count != int(count)):
            raise ValueError('conditional invalid sample count')
        counts.append(count)
    if len(set(counts)) != 1:
        raise ValueError('conditional event sample cohort mismatch')


def validate_pair_witness(normal, control):
    """Consume server-created side validations; absent side cannot publish gain."""
    pending = {'schema_version':1,'status':'pair_pending','valid':None,'reason':'conditional_pair_missing'}
    if normal is None or control is None: return pending
    try:
        if normal['status']!='pair_pending' or control['status']!='pair_pending': raise ValueError('side not verified')
        n,c = normal['conditional_witness'],control['conditional_witness']
        if n['mode']!='normal' or c['mode']!='control': raise ValueError('pair mode mismatch')
        validate_event_samples(n)
        validate_event_samples(c)
        keys = ('contract_hash','identity','factor','relative_tolerance','event_id','options_hash','context_hash')
        if 'pair_binding' in n or 'pair_binding' in c:
            for side in (n, c):
                binding = side['pair_binding']
                if (set(binding) != {'schema_version', 'input_hashes', 'proof_hash'}
                        or binding['schema_version'] != 2
                        or set(binding['input_hashes']) != {'normal', 'control'}):
                    raise ValueError('pair binding version/shape mismatch')
            keys += ('pair_binding',)
        else:
            # Durable legacy witnesses keep their original exact comparison.
            keys += ('pair_hash', 'input_hashes')
        for key in keys:
            if n[key]!=c[key]: raise ValueError('pair binding mismatch')
        if n['report_hash']==c['report_hash'] or n['input_hashes']['normal']==n['input_hashes']['control']:
            raise ValueError('duplicate pair')
        for key in ('num_executes','num_ticks','num_tick_results'):
            if n[key]!=c[key] or n[key]['sum']<=0 or n[key]['count']<=0: raise ValueError('event counts mismatch')
        if n['actual_amount']['count'] != c['actual_amount']['count'] or n['actual_amount']['count'] != n['num_ticks']['count']:
            raise ValueError('samples mismatch')
        if c['actual_amount']['sum']<=0: raise ValueError('consumer damage missing')
        ratio=n['actual_amount']['sum']/c['actual_amount']['sum']
        if not math.isfinite(ratio) or not math.isclose(ratio,n['factor'],rel_tol=n['relative_tolerance'],abs_tol=0):
            raise ValueError('consumer amplitude mismatch')
        return {'schema_version':1,'status':'valid','valid':True,'reason':'','comparison_kind':'conditional_increment',
            'damage_ratio':ratio,'event_id':n['event_id'],'normal':n,'control':c}
    except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError) as exc:
        return {'schema_version':1,'status':'invalid','valid':False,'reason':str(exc)}
