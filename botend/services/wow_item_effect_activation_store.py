"""Central, exact-build activation facts and compact execution references."""
from copy import deepcopy
import hashlib
import json
import re
from django.core.exceptions import ValidationError
from django.db import transaction
from botend.models import WowItemSnapshot

META_KEY = 'simc_effect_activation_by_build'
BUILD_RE = re.compile(r'\d+\.\d+\.\d+\.\d+')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def activation_reference(fact):
    if (not isinstance(fact, dict) or fact.get('schema_version') != 1
            or type(fact.get('item_id')) is not int or fact['item_id'] <= 0
            or not BUILD_RE.fullmatch(str(fact.get('game_build', '')))
            or ('requires_explicit_bonus_choice' in fact
                and type(fact['requires_explicit_bonus_choice']) is not bool)):
        raise ValidationError('中央装备激活事实格式无效')
    bonuses, effects = fact.get('required_bonus_ids'), fact.get('effects')
    if (not isinstance(bonuses, list) or not isinstance(effects, list)
            or any(type(b) is not int or b <= 0 for b in bonuses)
            or any(not isinstance(e, dict) or type(e.get('spell_id')) is not int
                   or e['spell_id'] <= 0 for e in effects)):
        raise ValidationError('中央装备激活身份无效')
    expected_effects = [effect for effect in effects if effect.get('bonus_id') in bonuses] or effects
    drivers = {effect['spell_id'] for effect in expected_effects}
    events = set(drivers)
    trigger_rows = [row for page in fact.get('source', {}).get('evidence', [])
                    if page.get('table') == 'SpellEffect' for row in page.get('rows', [])]
    for _ in range(len(trigger_rows) + 1):
        found = {row['EffectTriggerSpell'] for row in trigger_rows
                 if row.get('SpellID') in events and type(row.get('EffectTriggerSpell')) is int
                 and row['EffectTriggerSpell'] > 0}
        if found.issubset(events):
            break
        events.update(found)
    for proof in fact.get('native_event_evidence', []):
        if (proof.get('item_id') == fact['item_id'] and proof.get('game_build') == fact['game_build']
                and proof.get('driver_spell_id') in drivers and type(proof.get('event_spell_id')) is int
                and proof['event_spell_id'] > 0 and proof.get('report_sha256') and proof.get('simc_revision')):
            events.add(proof['event_spell_id'])
    return {'item_id': fact['item_id'], 'game_build': fact['game_build'],
            'required_bonus_ids': sorted(set(bonuses)),
            'driver_spell_ids': sorted(drivers),
            'event_spell_ids': sorted(events),
            'fact_hash': _hash(fact)}


@transaction.atomic
def merge_item_effect_activation(facts, *, is_ptr):
    """Update only central base metadata; never relabel or overwrite variants."""
    if type(is_ptr) is not bool:
        raise ValidationError('必须显式声明激活来源的正式服/PTR分支')
    refs = [activation_reference(fact) for fact in facts]
    objects = {item.item_id: item for item in WowItemSnapshot.objects.select_for_update()
               .filter(item_id__in=[ref['item_id'] for ref in refs]).only('id','item_id','metadata')}
    if set(objects) != {ref['item_id'] for ref in refs}:
        raise ValidationError('待补采装备不在中央目录中')
    changed = set()
    for fact, ref in zip(facts, refs):
        if fact.get('source', {}).get('provider') != 'wago_db2' or not fact['source'].get('evidence'):
            raise ValidationError('激活事实缺少已核实 DB2 来源')
        item = objects[ref['item_id']]
        metadata = deepcopy(item.metadata or {})
        entries = metadata.setdefault(META_KEY, {})
        entry = {'is_ptr': is_ptr, 'fact': deepcopy(fact)}
        if entries.get(ref['game_build']) != entry:
            entries[ref['game_build']] = entry
            item.metadata = metadata
            changed.add(item.item_id)
    for item_id in changed:
        objects[item_id].save(update_fields=['metadata'])
    return {'changed_item_ids': sorted(changed), 'references': refs}


def item_activation_facts(item_ids):
    # Fetch only the dedicated JSON fragment, not all catalog/tooltip metadata.
    from django.db.models.fields.json import KeyTransform
    rows = WowItemSnapshot.objects.filter(item_id__in=set(item_ids)).values_list(
        'item_id', KeyTransform(META_KEY, 'metadata'))
    return {item_id: facts if isinstance(facts, dict) else {} for item_id, facts in rows}


def select_activation(facts, *, is_ptr, item_id, game_build=''):
    from botend.services.wow_item_identity import build_key
    if type(is_ptr) is not bool:
        raise ValidationError('必须显式声明激活来源分支')
    if game_build:
        build_key(game_build)
    candidates = []
    for build, entry in facts.items():
        if game_build and build != game_build:
            continue
        if not BUILD_RE.fullmatch(str(build)):
            continue
        parts = tuple(map(int, build.split('.')))
        # Branch is explicitly supplied by the exact-build collection caller,
        # never guessed from version parity or the item's current display label.
        if not isinstance(entry, dict) or type(entry.get('is_ptr')) is not bool or entry['is_ptr'] != is_ptr:
            continue
        fact = entry.get('fact')
        if not isinstance(fact, dict) or fact.get('item_id') != item_id or fact.get('game_build') != build:
            raise ValidationError('中央装备激活来源身份不一致')
        candidates.append((parts, fact))
    return max(candidates, key=lambda row: row[0])[1] if candidates else None


def freeze_equipment_activation(params, facts_by_item):
    """Preserve every existing option; append only independently proven bonuses."""
    from simc_equipment_control import candidate_swaps, ALIASES
    result = deepcopy(params)
    targets = []
    for swap in candidate_swaps(result):
        item_id = swap.get('item_id')
        from botend.services.wow_item_identity import validate_swap_identity_context
        validate_swap_identity_context(swap, trusted_item_identity=True)
        fact = select_activation(facts_by_item.get(item_id, {}),
            is_ptr=swap.get('is_ptr') is True, item_id=item_id, game_build=swap.get('game_build', ''))
        if not fact:
            if swap.get('game_build') and facts_by_item.get(item_id):
                raise ValidationError('选定装备构建缺少同分支激活事实')
            continue  # Native initialization still fail-closes unloaded effects.
        ref = activation_reference(fact)
        raw = str(swap.get('raw_value') or '')
        matches = list(re.finditer(r'(?:^|,)bonus_id=([^,]*)', raw))
        if len(matches) > 1:
            raise ValidationError('装备行包含重复 bonus_id')
        existing = [int(v) for v in matches[0][1].split('/')] if matches else []
        required = ref['required_bonus_ids']
        # Optional crafting choices are not inherent effects of the carrier.
        if fact.get('requires_explicit_bonus_choice') is True and not set(required).issubset(existing):
            continue
        combined = list(dict.fromkeys(existing + required))
        if combined != existing:
            value = '/'.join(map(str, combined))
            if matches:
                match = matches[0]
                raw = raw[:match.start(1)] + value + raw[match.end(1):]
            else:
                raw += ',bonus_id=' + value
            swap['raw_value'] = raw
            swap['bonus_id'] = value
        targets.append({'slot': ALIASES.get(swap.get('slot'), swap.get('slot')), **ref})
    if targets:
        result['equipment_effect_expectation'] = {'schema_version': 1, 'targets': targets}
    return result
