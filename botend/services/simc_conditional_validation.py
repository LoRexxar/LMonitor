"""Strict conditional-only report adapter; historical validators are unchanged."""
import hashlib
import json
import re
from simc_equipment_conditional import (
    validate_contract, authorize_exports, digest, input_digest, validate_pair_witness,
    validate_event_samples,
)


def _profile_context(text):
    """Project saved actor options and APL, not comments/gear/report text.

    SimC player_t::create_profile saves sorted lists, with first '=' and then
    '+=/'. init_action_list splits the accumulated list string on '/'. Preserve
    action order/expressions within each list; list ordering is not semantic.
    This strict conditional scope requires explicit actor identity and APL;
    an unexpanded built-in APL cannot be inferred from the report under test.
    """
    from simc_equipment_control import ALL_SLOTS, ALIASES
    classes = {'death_knight', 'demon_hunter', 'druid', 'evoker', 'hunter',
               'mage', 'monk', 'paladin', 'priest', 'rogue', 'shaman', 'warlock', 'warrior'}
    fields, lists, actors = {}, {}, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([\w.]+)(\+?=)(.*)', line)
        if not match:
            raise ValueError('conditional unsupported context syntax')
        key, op, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if ALIASES.get(key, key) in ALL_SLOTS:
            continue
        if key in classes:
            actors.append((key, value))
        if key == 'actions' or key.startswith('actions.'):
            name = key.partition('.')[2] or 'default'
            lists[name] = (lists.get(name, '') if op == '+=' else '') + value
        else:
            if op != '=' or key in fields:
                raise ValueError('conditional ambiguous context option')
            fields[key] = value
    if len(actors) != 1 or not actors[0][1]:
        raise ValueError('conditional actor ambiguous')
    if any(not fields.get(k) for k in ('spec', 'level', 'race', 'role', 'position', 'talents')):
        raise ValueError('conditional explicit actor context required')
    apl = {name: [action for action in value.split('/') if action] for name, value in lists.items() if value}
    if not apl:
        raise ValueError('conditional explicit APL required')
    return actors[0], fields, apl


def _bind_report_context(prepared_input, profile, player, proof):
    actor, frozen, apl = _profile_context(prepared_input)
    report_actor, saved, saved_apl = _profile_context(profile)
    # Saved fields (including class-specific options) must come from the frozen
    # input, not from a second report. Run options absent from SAVE_PLAYER are
    # deliberately not compared to the entire raw Profile text.
    saved_keys = {'source', 'spec', 'level', 'race', 'role', 'position', 'talents',
                  'class_talents', 'spec_talents', 'hero_talents', 'omnium_talents',
                  'potion', 'flask', 'food', 'augmentation', 'temporary_enchant'}
    required = {k for k in frozen if k in saved_keys or k.startswith('apl_variable.')}
    # player_t defaults profile_source_ to DEFAULT and SAVE_PLAYER always
    # emits source=default (SimC 6c50c3c7 player.cpp / util.cpp). Only supply
    # that missing frozen default; explicit/unknown sources remain compared.
    frozen.setdefault('source', 'default')
    if (actor != report_actor or apl != saved_apl or not required <= saved.keys()
            or any(frozen.get(k) != v for k, v in saved.items())):
        raise ValueError('conditional frozen report context mismatch')
    class_name, name = actor
    specialization = (frozen['spec'] + class_name).replace('_', '').lower()
    # JSON uses the display form "Spec Class", unlike Profile's spec token.
    actual_spec = re.sub(r'[ _]', '', player['specialization']).lower()
    if (player['name'] != name or actual_spec != specialization
            or type(player['level']) is not int or str(player['level']) != frozen['level']
            or any(player[k] != frozen[k] for k in ('race', 'role', 'talents'))):
        raise ValueError('conditional JSON actor context mismatch')
    for group in ('original', 'normal', 'control'):
        snapshot = proof[group]
        log = snapshot['effect_log']
        # Native names are unescaped; only quote-plus-period at line end
        # terminates a name. Match the strict full-log boundaries used by v3.
        for line in log.splitlines():
            for marker in ('Initializing items for Player ',
                           'Initializing special effects for Player ',
                           'Creating Auras, Buffs, and Debuffs for Pet '):
                if marker in line and not re.fullmatch(r"'[^\r\n]+'\.", line.partition(marker)[2]):
                    raise ValueError('conditional native actor context mismatch')
        pets = set(re.findall(r"Creating Auras, Buffs, and Debuffs for Pet '([^\r\n]+)'\.$", log, re.MULTILINE))
        scopes = [a for a in re.findall(r"Initializing items for Player '([^\r\n]+)'\.$", log, re.MULTILINE) if a not in pets]
        effects = re.findall(r"Initializing special effects for Player '([^\r\n]+)'\.$", log, re.MULTILINE)
        if scopes != [name] or effects != [name]:
            raise ValueError('conditional native actor context mismatch')
        for item in snapshot['items'].values():
            for effect in item['effects']:
                if effect.get('origin') and effect['origin']['actor'] != name:
                    raise ValueError('conditional native origin actor mismatch')
    return {'actor': actor, 'profile': saved, 'apl': apl}


def _pair_output_binding(prepared_input, params, proof, control_exports):
    """Server-created v2 comparison, after all legacy side checks.

    Reconstruct BOTH exact inputs and verify their existing hashes first. Only
    literal, nonempty html/json2 destination values are then normalized. Keep
    option presence/order, all other bytes, and the entire remaining proof.
    json2 comma options are NOT paths (parse_json_reports); never erase them.
    """
    from simc_equipment_control import NATIVE_PROOF_MARKER, synthetic_item
    lines = [l for l in prepared_input.splitlines() if not l.startswith(NATIVE_PROOF_MARKER)]
    outputs = [l for l in lines if l.startswith(('html=', 'json2='))]
    if not outputs:
        return None  # Preserve legacy witness dictionaries exactly.
    swaps = {s['slot']: s['raw_value'] for s in params['gear_swaps']}
    changed = proof['policy']['changed_slots']
    inputs = {}
    for mode in ('normal', 'control'):
        content = list(lines)
        for slot in changed:
            indexes = [i for i, line in enumerate(content) if line.startswith(slot + '=')]
            if len(indexes) != 1:
                raise ValueError('conditional pair input slot ambiguous')
            content[indexes[0]] = slot + '=' + (swaps[slot] if mode == 'normal'
                                                       else synthetic_item(control_exports[slot]))
        text = '\n'.join(content)
        if input_digest(text) != proof['input_hashes'][mode]:
            raise ValueError('conditional pair full input binding')
        # Only a single literal path per output key is eligible. Empty values
        # change reporting behavior; complex syntax stays byte-for-byte bound.
        for key in ('html', 'json2'):
            indexes = [i for i, line in enumerate(content) if line.startswith(key + '=')]
            if len(indexes) == 1:
                i = indexes[0]
                if re.fullmatch(r'[A-Za-z0-9_./:\\-]+', content[i].partition('=')[2]):
                    content[i] = key + '=<output-path>'
        inputs[mode] = input_digest('\n'.join(content))
    return {'schema_version': 2, 'input_hashes': inputs,
            'proof_hash': digest({k: v for k, v in proof.items()
                                  if k not in ('mode', 'pair_hash', 'input_hashes')})}


def validate_conditional_report(report_html, params, proof, *, prepared_input=None,
                                report_json=None, conditional_authorization=None):
    from simc_equipment_control import (native_proof_marker, extract_native_proof, parse_equipment_export,
        native_effects_equal, _native_sets, embellishment_count, ALL_SLOTS, ALIASES)
    from botend.services.simc_equipment_result_evidence import _native_document, _slot
    from botend.services.simc_player_config import _parse_line
    mode = 'control' if params.get('equipment_effect_control') is True else 'normal'
    try:
        policy, expectation = params['equipment_effect_policy'], params['equipment_effect_expectation']
        relation = validate_contract(policy,expectation,authorization=conditional_authorization)
        native_proof_marker(proof)
        if (proof['schema_version'] != 4 or proof['mode'] != mode
                or proof['comparison_kind'] != 'conditional_increment'
                or proof['scope'] != 'equipment_effect_combination'
                or proof['policy'] != policy or proof['expectation'] != expectation
                or proof['identity'] != expectation['identity']
                or proof['contract_hash'] != digest({'policy':policy,'expectation':expectation})
                or proof['rules_hash'] != digest(policy['rules'])
                or proof['pair_hash'] != digest({k:v for k,v in proof.items() if k not in ('pair_hash','mode')})):
            raise ValueError('conditional proof binding')
        if (not prepared_input or input_digest(prepared_input) != proof['input_hashes'][mode]
                or extract_native_proof(prepared_input) != proof):
            raise ValueError('conditional prepared input binding')
        targets = {t['slot']:t for t in expectation['targets']}
        if proof['targets'] != [{'slot':s,'item_id':targets[s]['item_id']} for s in policy['target_slots']]:
            raise ValueError('conditional proof targets')
        swaps = params.get('gear_swaps',[])
        if len(swaps)!=2 or {s['slot'] for s in swaps}!=set(targets): raise ValueError('conditional swaps')
        for swap in swaps:
            target=targets[swap['slot']]
            _,_,fields=_parse_line('gear='+swap['raw_value'])
            if fields.get('id')!=str(target['item_id']) or swap.get('item_id',target['item_id'])!=target['item_id']:
                raise ValueError('conditional candidate identity')
        groups={}
        for group in ('original','normal','control'):
            snapshot=proof[group]; roster=snapshot['items']
            if not roster or not set(roster)<=ALL_SLOTS: raise ValueError('conditional roster')
            profile='\n'.join(s+'='+item['profile_value'] + ('\n# weapon='+item['static']['weapon'][0] if item['static']['weapon'] else '') for s,item in roster.items())
            exports={s:parse_equipment_export(profile,snapshot['effect_log'],s,schema_version=3) for s in roster}
            if group != 'control': authorize_exports(exports,expectation,relation)
            for s,item in roster.items():
                actual=exports[s]
                if digest(item['static']) != digest({k:actual[k] for k in ('stats','weapon','attachments')}) or item['effects']!=actual['effects']:
                    raise ValueError('conditional native snapshot tamper')
                _,_,fields=_parse_line('gear='+item['profile_value'])
                bonuses=sorted(set(map(int,re.findall(r'\d+',fields.get('bonus_id','')))))
                if item['item_id']!=int(fields.get('id','0')) or item['bonus_ids']!=bonuses:
                    raise ValueError('conditional native item tamper')
            # Compact effect logs do not include set lines. Preserve conservative
            # same-set gate for this minimal conditional scope (no set modifiers).
            groups[group]=exports
        original,normal,control=(proof[k]['items'] for k in ('original','normal','control'))
        if not set(original)==set(normal)==set(control): raise ValueError('conditional roster changed')
        background=set(proof['background_removed']); changed=set(policy['changed_slots'])
        if background & set(targets) or not background<=set(original): raise ValueError('conditional background')
        for slot,before in original.items():
            if not before['static']==normal[slot]['static']==control[slot]['static']: raise ValueError('conditional static changed')
            if slot in changed | background:
                if control[slot]['item_id'] or control[slot]['bonus_ids'] or control[slot]['effects']:
                    raise ValueError('conditional removal failed')
            else:
                if normal[slot]['profile_value']!=control[slot]['profile_value'] or not native_effects_equal(normal[slot]['effects'],control[slot]['effects']):
                    raise ValueError('conditional fixed context changed')
            if slot not in background and (before['profile_value']!=normal[slot]['profile_value'] or not native_effects_equal(before['effects'],normal[slot]['effects'])):
                raise ValueError('conditional normal changed')
            if slot in background and (normal[slot]['item_id'] or normal[slot]['effects']): raise ValueError('conditional background retained')
        if proof['normal']['sets']!=proof['control']['sets'] or proof['target_sets']:
            raise ValueError('conditional set changes unsupported')
        for group,expected_count in (('normal',2),('control',1)):
            counts=[embellishment_count(e,policy['rules']) for e in groups[group].values()]
            if sum(counts)!=expected_count or any(c>1 for c in counts): raise ValueError('conditional embellishment count')
        document,complete=_native_document(report_html)
        profile='\n'.join(b for s in document.get('sections',[]) if s.get('key')=='profile' for b in s.get('text_blocks',[]))
        actual={}
        for line in profile.splitlines():
            slot,_,fields=_parse_line(line);slot=ALIASES.get(slot,slot)
            if slot in ALL_SLOTS:
                if slot in actual: raise ValueError('conditional report duplicate gear')
                actual[slot]=fields
        selected=proof[mode]['items']
        if not complete or set(actual)!=set(selected): raise ValueError('conditional report incomplete')
        if any(actual[s]!=_parse_line('gear='+i['profile_value'])[2] for s,i in selected.items()): raise ValueError('conditional report mode/gear mismatch')
        if report_json is None: raise ValueError('conditional numeric report required')
        report=json.loads(report_json) if isinstance(report_json,(str,bytes)) else report_json
        sim=report['sim']; options=sim['options']; dbc=options['dbc'];identity=expectation['identity']
        if (dbc[dbc['version_used']]['wow_version']!=identity['dbc_build']
                or not re.fullmatch('[0-9a-f]{7,40}',report['git_revision'])
                or not identity['revision'].startswith(report['git_revision'])):
            raise ValueError('conditional report build/revision')
        if len(sim['players'])!=1: raise ValueError('conditional actor ambiguous')
        player=sim['players'][0]
        json_gear={ALIASES.get(s,s):_parse_line('gear='+g['encoded_item'])[2] for s,g in player['gear'].items()}
        if json_gear != actual:
            raise ValueError('conditional JSON/HTML mixed report gear')
        context = _bind_report_context(prepared_input, profile, player, proof)
        event_id=relation['consumer_event_spell_ids'][0]
        events=[s for s in player['stats'] if s.get('id')==event_id]
        if len(events)!=1 or events[0].get('item_id')!=targets[policy['context_slots'][0]]['item_id']:
            raise ValueError('conditional consumer event missing/identity')
        event=events[0]
        numeric={k:{f:event[k][f] for f in ('sum','count')} for k in ('num_executes','num_ticks','num_tick_results','actual_amount')}
        validate_event_samples(numeric)
        witness=dict(mode=mode,contract_hash=proof['contract_hash'],pair_hash=proof['pair_hash'],identity=identity,
            input_hashes=proof['input_hashes'],report_hash=digest(report),html_hash=hashlib.sha256(report_html.encode()).hexdigest(),
            factor=relation['factor'],relative_tolerance=relation['relative_tolerance'],event_id=event_id,
            context_hash=digest(context), options_hash=digest(options),**numeric)
        binding = _pair_output_binding(prepared_input, params, proof, groups['control'])
        if binding is not None:
            witness['pair_binding'] = binding
        return {'schema_version':2,'status':'pair_pending','valid':None,'mode':mode,'reason':'conditional_pair_required',
                'validation_basis':'conditional_native_structure','conditional_witness':witness}
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError,RecursionError,IndexError) as exc:
        return {'schema_version':2,'status':'invalid','valid':False,'mode':mode,'reason':'conditional_invalid: '+str(exc)}
