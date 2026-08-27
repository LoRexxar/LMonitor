import copy
import json
import math
import os
import re
import subprocess
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, models
from django.utils import timezone

from botend.constants.hero_talents import (
    hero_subtree_name_by_id, hero_subtree_name_zh, spec_hero_subtree_names,
)
from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, SimcBackendBinary, SimcProfile,
    SimcSkillDamageSnapshot, WowSpellSnapshot, WowTalentNodeMetadata,
)
from botend.services.simc_composer import SimcComposer
from botend.services.simc_player_config import (
    EQUIPMENT_SLOT_ALIASES, EQUIPMENT_SLOTS, canonical_simc_profile_identity,
    simc_spec_slug,
)
from botend.wow.talents.metadata import TalentMetadataProvider


def _text_key(value):
    return str(value or '').strip().casefold()


def _contains_cjk(value):
    return bool(re.search(r'[\u3400-\u9fff]', str(value or '')))


_HAND_COMPONENT_SUFFIX_RE = re.compile(
    r'_(?P<hand>mh|oh|main_hand|off_hand)$', re.IGNORECASE,
)


def _hand_component_identity(value):
    """Return a canonical base token and hand for terminal SimC hand suffixes."""
    token = _text_key(value)
    match = _HAND_COMPONENT_SUFFIX_RE.search(token)
    if not match or not token[:match.start()]:
        return '', ''
    hand = 'main' if match.group('hand').lower() in ('mh', 'main_hand') else 'off'
    return token[:match.start()], hand


def _action_hand_component_identity(action):
    for value in (action.get('token'), action.get('name')):
        base_token, hand = _hand_component_identity(value)
        if base_token:
            return base_token, hand
    return '', ''


def _action_variant_ownership_key(action):
    variant = action.get('variant') or {}
    hero_subtree_ids = tuple(sorted({
        subtree_id
        for subtree_id in (action.get('hero_subtree_ids') or [])
        if (
            isinstance(subtree_id, int)
            and not isinstance(subtree_id, bool)
            and subtree_id > 0
        )
    }))
    return (
        json.dumps(variant, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        hero_subtree_ids,
    )


def _single_top_name(rows, rank):
    ranked = [(rank(row), str(row.get('name_zh') or '').strip()) for row in rows]
    ranked = [(item_rank, name) for item_rank, name in ranked if item_rank >= 0 and name]
    if not ranked:
        return ''
    top_rank = max(item_rank for item_rank, _name in ranked)
    names = {name for item_rank, name in ranked if item_rank == top_rank}
    return next(iter(names)) if len(names) == 1 else ''


def localize_skill_damage_payload(payload):
    """Add read-time Chinese action labels without changing the exporter snapshot."""
    result = copy.deepcopy(payload or {})
    actors = [row for row in (result.get('actors') or []) if isinstance(row, dict)]
    spell_ids = {
        spell_id
        for actor in actors for action in (actor.get('actions') or [])
        if isinstance(action, dict)
        for spell_id in (action.get('spell_id'), action.get('reporting_root_spell_id'))
        if isinstance(spell_id, int) and not isinstance(spell_id, bool) and spell_id > 0
    }
    spell_ids.update(
        spell_id
        for actor in actors
        for effect in (actor.get('global_skill_effects') or [])
        if isinstance(effect, dict)
        for spell_id in (effect.get('source_spell_ids') or [])
        if isinstance(spell_id, int) and not isinstance(spell_id, bool) and spell_id > 0
    )
    tokens = set()
    for actor in actors:
        for action in actor.get('actions') or []:
            if not isinstance(action, dict):
                continue
            token = _text_key(action.get('token'))
            if token:
                tokens.add(token)
            root_token = _text_key(action.get('reporting_root_token'))
            if root_token:
                tokens.add(root_token)
            base_token, _hand = _action_hand_component_identity(action)
            if base_token:
                tokens.add(base_token)
    snapshot_build = str((payload.get('identity') or {}).get('game_build') or '').strip()
    spell_query = WowSpellSnapshot.objects.filter(
        branch='wow', locale='zhCN', spell_id__in=spell_ids, name_zh__gt='',
    )
    if snapshot_build:
        spell_query = spell_query.filter(snapshot_build=snapshot_build)
    spell_names = {
        row['spell_id']: str(row['name_zh'] or '').strip()
        for row in spell_query.values('spell_id', 'name_zh')
    } if spell_ids else {}
    missing_spell_ids = spell_ids - set(spell_names)
    if missing_spell_ids:
        recent_spell_names = WowSpellSnapshot.objects.filter(
            branch='wow', locale='zhCN', spell_id__in=missing_spell_ids,
            name_zh__gt='',
        ).values('spell_id', 'name_zh').order_by('-updated_at')
        for row in recent_spell_names:
            spell_names.setdefault(row['spell_id'], str(row['name_zh'] or '').strip())
    talent_rows = list(
        WowTalentNodeMetadata.objects.filter(
            talent_version__is_active=True,
            name_zh__gt='',
        ).filter(
            models.Q(spell_id__in=spell_ids) | models.Q(display_spell_id__in=spell_ids)
        ).values(
            'spell_id', 'display_spell_id', 'class_name', 'spec_name', 'name_zh',
        )
    ) if spell_ids else []
    apl_rows = list(
        SimcAplSymbolScope.objects.filter(
            is_active=True,
            symbol__is_active=True,
            symbol__symbol_kind=SimcAplSymbol.KIND_ACTION,
            name_zh__gt='',
        ).filter(
            models.Q(symbol__token__in=tokens) | models.Q(spell_id__in=spell_ids)
        ).values(
            'symbol__token', 'spell_id', 'class_name', 'spec', 'hero_tree', 'name_zh',
        )
    ) if tokens or spell_ids else []

    for actor in actors:
        class_key = _text_key(actor.get('class'))
        spec_key = _text_key(actor.get('specialization'))
        hero_key = _text_key(actor.get('hero_talent_tree'))

        def scope_rank(row, *, talent=False):
            row_class = _text_key(row.get('class_name'))
            row_spec = _text_key(row.get('spec_name') if talent else row.get('spec'))
            row_hero = '' if talent else _text_key(row.get('hero_tree'))
            if row_class and row_class != class_key:
                return -1
            if row_spec and row_spec != spec_key:
                return -1
            if row_hero and row_hero != hero_key:
                return -1
            return (4 if row_hero else 0) + (2 if row_spec else 0) + (1 if row_class else 0)

        for effect in actor.get('global_skill_effects') or []:
            if not isinstance(effect, dict):
                continue
            source_name = next((
                spell_names.get(spell_id)
                for spell_id in (effect.get('source_spell_ids') or [])
                if spell_names.get(spell_id)
            ), '')
            source_token = str(effect.get('source_token') or '').partition('.')[2]
            effect['display_name'] = (
                str(effect.get('talent_name_zh') or '').strip()
                or str(effect.get('talent_name') or '').strip()
                or source_name
                or source_token.replace('_', ' ').strip()
                or '未命名全局效果'
            )

        for action in actor.get('actions') or []:
            if not isinstance(action, dict):
                continue
            token = _text_key(action.get('token'))
            spell_id = action.get('spell_id')
            base_token, _hand = _action_hand_component_identity(action)
            root_token = _text_key(action.get('reporting_root_token'))
            root_spell_id = action.get('reporting_root_spell_id')
            action_identity = {token, _text_key(action.get('name'))}
            has_distinct_reporting_root = (
                action.get('reporting_root_component') is True
                and root_token
                and root_token not in action_identity
            )
            if has_distinct_reporting_root:
                canonical_token = root_token
                canonical_spell_id = root_spell_id
            elif base_token:
                canonical_token = base_token
                canonical_spell_id = None
            else:
                canonical_token = token
                canonical_spell_id = spell_id
            apl_token_name = _single_top_name(
                [
                    row for row in apl_rows
                    if _text_key(row.get('symbol__token')) == canonical_token
                ],
                scope_rank,
            ) if canonical_token else ''
            talent_name = _single_top_name(
                [row for row in talent_rows if canonical_spell_id in (
                    row.get('spell_id'), row.get('display_spell_id'),
                )],
                lambda row: scope_rank(row, talent=True),
            ) if isinstance(canonical_spell_id, int) else ''
            apl_spell_name = _single_top_name(
                [row for row in apl_rows if row.get('spell_id') == canonical_spell_id],
                scope_rank,
            ) if isinstance(canonical_spell_id, int) else ''
            existing_display_name = str(action.get('display_name') or '').strip()
            existing_localized_name = (
                existing_display_name if _contains_cjk(existing_display_name) else ''
            )
            if base_token and (
                _hand_component_identity(existing_display_name)[0]
                or _text_key(existing_display_name) in action_identity
            ):
                existing_display_name = ''
            action['display_name'] = (
                existing_localized_name
                or spell_names.get(canonical_spell_id)
                or apl_token_name or talent_name or apl_spell_name
                or spell_names.get(spell_id)
                or existing_display_name
                or str(action.get('name') or action.get('token') or '未命名技能')
            )
    return result


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def build_single_talent_actor_input(
    profile_input, class_name, talents, *, scaffold_talents=(), talent_prerequisites=None,
):
    """Expand one reference actor into prerequisite-vs-selected actor pairs."""
    lines = str(profile_input or '').splitlines()
    actor_pattern = re.compile(rf'^{re.escape(str(class_name or "").strip())}="[^"]*"$')
    actor_index = next((index for index, line in enumerate(lines) if actor_pattern.match(line.strip())), None)
    if actor_index is None:
        raise ValueError('SimC Profile 缺少可识别的职业 actor 行。')
    global_lines = lines[:actor_index]
    actor_lines = [
        line for line in lines[actor_index:]
        if not re.match(r'^\s*(?:talents|class_talents|spec_talents|hero_talents)\s*=', line)
        and not re.match(r'^\s*html\s*=', line)
    ]
    equipment_slots = '|'.join(sorted({
        *(re.escape(slot) for slot in EQUIPMENT_SLOTS),
        *(re.escape(slot) for slot in EQUIPMENT_SLOT_ALIASES),
    }))
    equipment_line_pattern = re.compile(rf'^(\s*)({equipment_slots})\s*=\s*(.*)$')
    equipment_id_pattern = re.compile(r'^(?:[^,]*,)?id=(\d+)(.*)$')
    sanitized_actor_lines = []
    for line in actor_lines:
        equipment_match = equipment_line_pattern.match(line)
        if not equipment_match:
            sanitized_actor_lines.append(line)
            continue
        slot = EQUIPMENT_SLOT_ALIASES.get(equipment_match.group(2), equipment_match.group(2))
        if slot not in {'main_hand', 'off_hand'}:
            continue
        item_value = equipment_match.group(3)
        item_match = equipment_id_pattern.match(item_value)
        if not item_match:
            continue
        line = (
            f'{equipment_match.group(1)}{equipment_match.group(2)}='
            f',id={item_match.group(1)}{item_match.group(2)}'
        )
        sanitized_actor_lines.append(line)
    actor_lines = sanitized_actor_lines

    def actor_block(name, selected_talents=()):
        block = list(actor_lines)
        block[0] = f'{class_name}="{name}"'
        selected_talents = list(selected_talents)
        replacement_talent_ids = {
            getattr(trait, 'talent_id', None)
            for trait in selected_talents
            if isinstance(getattr(trait, 'talent_id', None), int)
            and getattr(trait, 'talent_id', None) > 0
        }
        selected = [
            trait for trait in scaffold_talents
            if getattr(trait, 'talent_id', None) not in replacement_talent_ids
        ]
        selected.extend(selected_talents)
        entries_by_option = {'class_talents': [], 'spec_talents': [], 'hero_talents': []}
        seen = set()
        for selected_trait in selected:
            tree_type = str(getattr(selected_trait, 'tree_type', '') or '').strip().lower()
            option = {'class': 'class_talents', 'spec': 'spec_talents', 'hero': 'hero_talents'}.get(tree_type)
            entry_id = getattr(selected_trait, 'node_id', None)
            rank = max(1, int(getattr(selected_trait, 'max_points', 1) or 1))
            identity = (option, entry_id)
            if not option or not isinstance(entry_id, int) or entry_id <= 0:
                raise ValueError('单项天赋缺少有效 tree_type 或 SimC trait entry。')
            if identity in seen:
                continue
            seen.add(identity)
            entries_by_option[option].append(f'{entry_id}:{rank}')
        for option, entries in entries_by_option.items():
            if entries:
                block.append(f'{option}={"/".join(entries)}')
        return block

    talent_prerequisites = talent_prerequisites or {}
    scaffold_identities = {
        (
            str(getattr(trait, 'tree_type', '') or '').strip().lower(),
            getattr(trait, 'node_id', None),
        )
        for trait in scaffold_talents
    }
    output = [*global_lines, *actor_block('skill_damage_base')]
    for trait in talents:
        identity = (
            str(getattr(trait, 'tree_type', '') or '').strip().lower(),
            getattr(trait, 'node_id', None),
        )
        if identity in scaffold_identities:
            continue
        prerequisites = list(talent_prerequisites.get(trait.pk) or [])
        identity = f'{trait.pk}_trait_{trait.node_id}'
        output.extend(actor_block(f'skill_damage_reference_{identity}', prerequisites))
        output.extend(actor_block(f'skill_damage_talent_{identity}', [*prerequisites, trait]))
    return '\n'.join(output).rstrip() + '\n'


def _action_identity(action):
    return (str(action.get('token') or ''), action.get('spell_id'))


def _scenario_tokens(scenario):
    if isinstance(scenario.get('active_buffs'), list):
        tokens = scenario['active_buffs']
    else:
        tokens = [
            buff.get('token')
            for buff in (scenario.get('buffs') or [])
            if isinstance(buff, dict)
        ]
    return tuple(sorted({str(token).strip() for token in tokens if str(token or '').strip()}))


_AMOUNT_COMPONENT_FIELDS = (
    'hit', 'crit', 'crit_multiplier', 'crit_chance', 'expected',
    'damage_equivalent_count',
)
_AMOUNT_COMPONENT_SIGNATURE_FIELDS = (
    *_AMOUNT_COMPONENT_FIELDS,
    'crit_chance_uncapped', 'can_crit', 'base_damage_layers', 'runtime_layers',
)


def _amount_expected(amount):
    """Return a display-only aggregate; never use it to decide whether facts differ."""
    if not isinstance(amount, dict) or amount.get('unresolved_reason'):
        return None
    values = []
    for component_name in ('direct', 'tick'):
        component = amount.get(component_name)
        if isinstance(component, dict) and _finite_number(component.get('expected')):
            values.append(float(component['expected']))
    return sum(values) if values else None


def _amount_state(amount):
    if not isinstance(amount, dict):
        return ('absent', '')
    if amount.get('unresolved_reason'):
        return ('unresolved', str(amount.get('unresolved_reason') or 'unknown'))
    return ('resolved', '')


def _amount_signature(amount):
    state = _amount_state(amount)
    if state[0] != 'resolved':
        return state
    components = []
    for component_name in ('direct', 'tick'):
        component = amount.get(component_name)
        if not isinstance(component, dict):
            components.append((component_name, 'absent'))
            continue
        components.append((
            component_name,
            'values',
            tuple((field, component.get(field)) for field in _AMOUNT_COMPONENT_SIGNATURE_FIELDS),
        ))
    return ('resolved', tuple(components))


def _effect_delta(reference, current):
    """Keep component values when facts are introduced, and deltas otherwise."""
    reference_state = _amount_state(reference)
    current_state = _amount_state(current)
    if reference_state[0] != 'resolved' or current_state[0] != 'resolved':
        return ('state', _amount_signature(reference), _amount_signature(current))

    components = []
    for component_name in ('direct', 'tick'):
        left = reference.get(component_name)
        right = current.get(component_name)
        if not isinstance(left, dict) or not isinstance(right, dict):
            components.append((component_name, 'state', isinstance(left, dict), isinstance(right, dict)))
            continue
        fields = []
        for field in _AMOUNT_COMPONENT_SIGNATURE_FIELDS:
            left_value = left.get(field)
            right_value = right.get(field)
            if _finite_number(left_value) and _finite_number(right_value):
                fields.append((field, 'delta', float(right_value) - float(left_value)))
            else:
                fields.append((field, 'value', left_value, right_value))
        components.append((component_name, 'values', tuple(fields)))
    return ('resolved', tuple(components))


def _fact_equal(left, right):
    if _finite_number(left) and _finite_number(right):
        return math.isclose(float(left), float(right), rel_tol=1e-8, abs_tol=1e-8)
    if type(left) is not type(right):
        return False
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(_fact_equal(a, b) for a, b in zip(left, right))
    return left == right


def _effect_changed(reference, current):
    return not _fact_equal(_effect_delta(reference, current), _effect_delta(reference, reference))


def _paired_effect_changed(high_reference, high_current, low_reference, low_current):
    return not _fact_equal(
        _effect_delta(high_reference, high_current),
        _effect_delta(low_reference, low_current),
    )


def _scenario_amounts(action):
    amounts = {}
    for scenario in ((action or {}).get('scenarios') or []):
        if not isinstance(scenario, dict):
            continue
        tokens = _scenario_tokens(scenario)
        if not tokens:
            continue
        amount = scenario.get('values') or scenario.get('amount')
        if tokens in amounts and not _fact_equal(
            _amount_signature(amounts[tokens]), _amount_signature(amount),
        ):
            raise ValueError(f'exporter 同一 scenario tokens 返回冲突数值：{" + ".join(tokens)}')
        amounts.setdefault(tokens, amount)
    return amounts


def _resolved_component_hits(actor):
    hits = {}
    for action in (actor or {}).get('actions') or []:
        if not isinstance(action, dict) or action.get('supported') is not True:
            continue
        amount = action.get('baseline')
        if _amount_state(amount)[0] != 'resolved':
            continue
        for component_name in ('direct', 'tick'):
            component = amount.get(component_name) if isinstance(amount, dict) else None
            hit = component.get('hit') if isinstance(component, dict) else None
            if _finite_number(hit) and abs(hit) > 1.0e-12:
                hits[(_action_identity(action), component_name)] = float(hit)
    return hits


def _uniform_amount_ratios(reference, selected):
    """Return damage ratios only when the whole amount is a pure multiplier."""
    if _amount_state(reference)[0] != 'resolved' or _amount_state(selected)[0] != 'resolved':
        return None
    ratios = []
    present = False
    for component_name in ('direct', 'tick'):
        left = reference.get(component_name)
        right = selected.get(component_name)
        if isinstance(left, dict) != isinstance(right, dict):
            return None
        if not isinstance(left, dict):
            continue
        present = True
        if any(
            not _finite_number(side.get(field))
            for side in (left, right)
            for field in _AMOUNT_COMPONENT_FIELDS
        ):
            return None
        if left['damage_equivalent_count'] <= 0 or right['damage_equivalent_count'] <= 0:
            return None
        for field in ('crit_multiplier', 'crit_chance', 'damage_equivalent_count'):
            if not _fact_equal(left[field], right[field]):
                return None
        for field in ('hit', 'crit', 'expected'):
            left_value = float(left[field])
            right_value = float(right[field])
            if abs(left_value) <= 1.0e-12:
                if abs(right_value) > 1.0e-12:
                    return None
            else:
                ratios.append(right_value / left_value)
    return ratios if present else None


_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE = 1.0e-5
_RUNTIME_LAYER_FIELDS = {
    'direct': (
        'da_multiplier', 'player_multiplier', 'versus_multiplier',
        'persistent_multiplier', 'target_da_multiplier', 'versatility',
        'pet_multiplier', 'target_pet_multiplier',
    ),
    'tick': (
        'ta_multiplier', 'player_multiplier', 'versus_multiplier',
        'persistent_multiplier', 'target_ta_multiplier', 'versatility',
        'pet_multiplier', 'target_pet_multiplier',
    ),
}


def _runtime_layer_family(layer):
    return {
        'da_multiplier': 'action_multiplier',
        'ta_multiplier': 'action_multiplier',
        'target_da_multiplier': 'target_action_multiplier',
        'target_ta_multiplier': 'target_action_multiplier',
    }.get(layer, layer)


def _player_skill_actions(actor):
    actions = {}
    for action in ((actor or {}).get('actions') or []):
        if not isinstance(action, dict) or action.get('supported') is not True:
            continue
        if action.get('player_skill') is not True or action.get('harmful') is not True:
            continue
        baseline = action.get('baseline')
        if _amount_state(baseline)[0] != 'resolved':
            continue
        has_positive_damage = any(
            isinstance((baseline or {}).get(component), dict)
            and _finite_number((baseline or {})[component].get('expected'))
            and (baseline or {})[component]['expected'] > 0.0
            for component in ('direct', 'tick')
        )
        if not has_positive_damage:
            continue
        identity = _action_identity(action)
        if identity in actions:
            return None
        actions[identity] = action
    return actions or None


def _runtime_layer_changes(reference_component, selected_component, component_name):
    if not isinstance(reference_component, dict) or not isinstance(selected_component, dict):
        return None
    reference_layers = reference_component.get('runtime_layers')
    selected_layers = selected_component.get('runtime_layers')
    if not isinstance(reference_layers, dict) or not isinstance(selected_layers, dict):
        return None
    fields = _RUNTIME_LAYER_FIELDS.get(component_name)
    if not fields:
        return None
    changes = []
    for field in fields:
        reference_value = reference_layers.get(field)
        selected_value = selected_layers.get(field)
        if (
            isinstance(reference_value, bool) or isinstance(selected_value, bool)
            or not isinstance(reference_value, (int, float))
            or not isinstance(selected_value, (int, float))
            or not math.isfinite(reference_value) or not math.isfinite(selected_value)
            or reference_value <= 0.0 or selected_value <= 0.0
        ):
            return None
        changes.append((field, selected_value / reference_value))
    return changes


def _action_runtime_layer_changes(reference_amount, selected_amount):
    if _amount_state(reference_amount)[0] != 'resolved' or _amount_state(selected_amount)[0] != 'resolved':
        return None
    changes = []
    for component_name in ('direct', 'tick'):
        reference_component = reference_amount.get(component_name)
        selected_component = selected_amount.get(component_name)
        if reference_component is None and selected_component is None:
            continue
        component_changes = _runtime_layer_changes(
            reference_component, selected_component, component_name,
        )
        if component_changes is None:
            return None
        changes.extend((component_name, field, ratio) for field, ratio in component_changes)
    return changes or None


def _global_evidence_action_pair(reference_actor, selected_actor):
    reference_actions = _player_skill_actions(reference_actor)
    selected_actions = _player_skill_actions(selected_actor)
    if not reference_actions or not selected_actions:
        return None

    shared_identities = set(reference_actions) & set(selected_actions)
    if not shared_identities:
        return None

    def reporting_root(action):
        return (
            action.get('reporting_root_token'),
            action.get('reporting_root_spell_id'),
        )

    reference_only_roots = {
        reporting_root(action)
        for identity, action in reference_actions.items()
        if identity not in shared_identities
    }
    selected_only_roots = {
        reporting_root(action)
        for identity, action in selected_actions.items()
        if identity not in shared_identities
    }
    reference_only_roots.discard((None, None))
    selected_only_roots.discard((None, None))
    if reference_only_roots & selected_only_roots:
        return None

    return (
        {identity: reference_actions[identity] for identity in shared_identities},
        {identity: selected_actions[identity] for identity in shared_identities},
    )


def _scenario_token_universe(actor):
    tokens = set()
    for action in (_player_skill_actions(actor) or {}).values():
        tokens.update(_scenario_amounts(action))
    return tokens


def _canonical_runtime_state_token(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value or '').lower()).strip('_')


def _scenario_is_selected_only_action_state(
    reference_actor, selected_actor, scenario_tokens,
):
    if len(scenario_tokens) != 1:
        return False
    scenario_token = str(scenario_tokens[0] or '')
    scope, separator, state_name = scenario_token.partition('.')
    if separator != '.' or scope not in {'buff', 'debuff'}:
        return False
    state_token = _canonical_runtime_state_token(state_name)
    if not state_token:
        return False

    def initialized_action_tokens(actor):
        tokens = set()
        for action in ((actor or {}).get('actions') or []):
            if not isinstance(action, dict):
                continue
            for field in ('token', 'action_token', 'name'):
                token = _canonical_runtime_state_token(action.get(field))
                if token:
                    tokens.add(token)
        return tokens

    reference_tokens = initialized_action_tokens(reference_actor)
    selected_tokens = initialized_action_tokens(selected_actor)
    return state_token in selected_tokens - reference_tokens


def _amount_is_explained_by_global_ratio(reference_amount, selected_amount, multiplier):
    scaled_fields = ('hit', 'crit', 'expected')
    unchanged_fields = ('crit_multiplier', 'crit_chance', 'damage_equivalent_count')
    for component_name in ('direct', 'tick'):
        reference_component = (reference_amount or {}).get(component_name)
        selected_component = (selected_amount or {}).get(component_name)
        if reference_component is None and selected_component is None:
            continue
        if not isinstance(reference_component, dict) or not isinstance(selected_component, dict):
            return False
        for field in scaled_fields:
            reference_value = reference_component.get(field)
            selected_value = selected_component.get(field)
            if not _finite_number(reference_value) or not _finite_number(selected_value):
                return False
            if math.isclose(
                reference_value, 0.0,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ):
                if not math.isclose(
                    selected_value, 0.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    return False
            elif not math.isclose(
                selected_value / reference_value, multiplier,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ):
                return False
        for field in unchanged_fields:
            reference_value = reference_component.get(field)
            selected_value = selected_component.get(field)
            if (
                not _finite_number(reference_value)
                or not _finite_number(selected_value)
                or reference_value != selected_value
            ):
                return False
    return True


def _runtime_layer_candidate(reference_actor, selected_actor, scenario_tokens):
    action_pair = _global_evidence_action_pair(reference_actor, selected_actor)
    if action_pair is None:
        return None
    reference_actions, selected_actions = action_pair

    groups = []
    compared_roots = set()
    unchanged_component_count = 0
    compared_component_count = 0
    for identity in reference_actions:
        reference_action = reference_actions[identity]
        selected_action = selected_actions[identity]
        reference_root = (
            reference_action.get('reporting_root_token'),
            reference_action.get('reporting_root_spell_id'),
        )
        selected_root = (
            selected_action.get('reporting_root_token'),
            selected_action.get('reporting_root_spell_id'),
        )
        if reference_root != selected_root:
            return None
        reference_amount = reference_action.get('baseline')
        selected_amount = selected_action.get('baseline')
        if scenario_tokens:
            selected_amount = _scenario_amounts(selected_action).get(scenario_tokens)
            if selected_amount is None:
                return None
        changes = _action_runtime_layer_changes(reference_amount, selected_amount)
        if changes is None:
            return None
        root = selected_root
        if not root[0] and not root[1]:
            return None
        compared_roots.add(root)
        for component_name, layer, ratio in changes:
            compared_component_count += 1
            if math.isclose(
                ratio, 1.0,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ):
                unchanged_component_count += 1
                continue
            if not math.isfinite(ratio) or ratio <= 0.0:
                return None
            for group in groups:
                if layer == group['layer'] and math.isclose(
                    ratio, group['ratio'],
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    group['ratios'].append(ratio)
                    group['roots'].add(root)
                    group['components'].add(component_name)
                    break
            else:
                groups.append({
                    'layer': layer,
                    'ratio': ratio,
                    'ratios': [ratio],
                    'roots': {root},
                    'components': {component_name},
                })

    corroborated = [
        group for group in groups
        if group['ratio'] > 1.0 + _GLOBAL_DAMAGE_RATIO_REL_TOLERANCE
        and len(compared_roots) >= 2
        and group['roots'] == compared_roots
    ]
    if len(corroborated) != 1:
        return None
    group = corroborated[0]
    conflicting_groups = [candidate for candidate in groups if candidate is not group]
    if conflicting_groups:
        # A selected talent can contain a focused skill effect in addition to its
        # global layer. Without an exporter-provided neutralized amount Django
        # cannot remove only the global factor, so classification must fail closed
        # instead of deleting or numerically dividing the focused skill row.
        return None
    for identity in reference_actions:
        reference_amount = reference_actions[identity].get('baseline')
        selected_amount = selected_actions[identity].get('baseline')
        if scenario_tokens:
            selected_amount = _scenario_amounts(selected_actions[identity]).get(scenario_tokens)
        if not _amount_is_explained_by_global_ratio(
            reference_amount, selected_amount, group['ratio'],
        ):
            return None
    return {
        'runtime_layer': group['layer'],
        'runtime_components': sorted(group['components']),
        'multiplier': sum(group['ratios']) / len(group['ratios']),
        'evidence_roots': [
            {'token': token, 'spell_id': spell_id}
            for token, spell_id in sorted(
                group['roots'], key=lambda item: (str(item[0]), str(item[1])),
            )
        ],
        'evidence_root_count': len(group['roots']),
        'evidence_component_count': len(group['ratios']),
        'compared_component_count': compared_component_count,
        'unchanged_component_count': unchanged_component_count,
    }


def _scenario_effect_ratio(baseline_value, scenario_value):
    if not _finite_number(baseline_value) or not _finite_number(scenario_value):
        return None
    if math.isclose(
        baseline_value, 0.0,
        rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
        abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
    ):
        return 1.0 if math.isclose(
            scenario_value, 0.0,
            rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
        ) else None
    ratio = scenario_value / baseline_value
    return ratio if math.isfinite(ratio) and ratio > 0.0 else None


def _scenario_marginal_ratio(
    reference_baseline_value, reference_scenario_value,
    selected_baseline_value, selected_scenario_value,
):
    reference_effect = _scenario_effect_ratio(
        reference_baseline_value, reference_scenario_value,
    )
    selected_effect = _scenario_effect_ratio(
        selected_baseline_value, selected_scenario_value,
    )
    if reference_effect is None or selected_effect is None:
        return None
    ratio = selected_effect / reference_effect
    return ratio if math.isfinite(ratio) and ratio > 0.0 else None


def _scenario_marginal_amount_is_explained(
    reference_baseline, reference_scenario,
    selected_baseline, selected_scenario, multiplier,
):
    # The runtime damage layer may coexist with an independently modelled crit-
    # chance effect. Only raw hit/crit amounts must scale with the isolated
    # damage layer; expected damage and crit chance deliberately retain that
    # additional per-skill effect for the normal talent rows.
    scaled_fields = ('hit', 'crit')
    unchanged_fields = ('crit_multiplier', 'damage_equivalent_count')
    for component_name in ('direct', 'tick'):
        rb_component = (reference_baseline or {}).get(component_name)
        rs_component = (reference_scenario or {}).get(component_name)
        sb_component = (selected_baseline or {}).get(component_name)
        ss_component = (selected_scenario or {}).get(component_name)
        components = (rb_component, rs_component, sb_component, ss_component)
        if all(component is None for component in components):
            continue
        if not all(isinstance(component, dict) for component in components):
            return False
        if all(
            all(
                _finite_number(component.get(field))
                and math.isclose(
                    component.get(field), 0.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                )
                for field in ('hit', 'crit', 'expected')
            )
            for component in components
        ):
            continue
        for field in scaled_fields:
            ratio = _scenario_marginal_ratio(
                rb_component.get(field), rs_component.get(field),
                sb_component.get(field), ss_component.get(field),
            )
            if ratio is None or not math.isclose(
                ratio, multiplier,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ):
                return False
        for field in unchanged_fields:
            ratio = _scenario_marginal_ratio(
                rb_component.get(field), rs_component.get(field),
                sb_component.get(field), ss_component.get(field),
            )
            if ratio is None or not math.isclose(
                ratio, 1.0, rel_tol=1.0e-12, abs_tol=1.0e-12,
            ):
                return False
    return True


def _runtime_layer_scenario_candidate(
    reference_actor, selected_actor, scenario_tokens,
    *, reference_scenario_available=True, allow_reduction=False,
):
    action_pair = _global_evidence_action_pair(reference_actor, selected_actor)
    if action_pair is None:
        return None
    reference_actions, selected_actions = action_pair

    groups = []
    compared_roots = set()
    unchanged_component_count = 0
    compared_component_count = 0
    for identity in reference_actions:
        reference_action = reference_actions[identity]
        selected_action = selected_actions[identity]
        reference_root = (
            reference_action.get('reporting_root_token'),
            reference_action.get('reporting_root_spell_id'),
        )
        selected_root = (
            selected_action.get('reporting_root_token'),
            selected_action.get('reporting_root_spell_id'),
        )
        if reference_root != selected_root or (not selected_root[0] and not selected_root[1]):
            return None

        reference_baseline = reference_action.get('baseline')
        selected_baseline = selected_action.get('baseline')
        reference_scenario = (
            _scenario_amounts(reference_action).get(
                scenario_tokens, reference_baseline,
            )
            if reference_scenario_available else reference_baseline
        )
        selected_scenario = _scenario_amounts(selected_action).get(scenario_tokens)
        if selected_scenario is None:
            continue
        reference_changes = _action_runtime_layer_changes(
            reference_baseline, reference_scenario,
        )
        selected_changes = _action_runtime_layer_changes(
            selected_baseline, selected_scenario,
        )
        if reference_changes is None or selected_changes is None:
            return None
        reference_change_map = {
            (component, layer): ratio for component, layer, ratio in reference_changes
        }
        selected_change_map = {
            (component, layer): ratio for component, layer, ratio in selected_changes
        }
        if set(reference_change_map) != set(selected_change_map):
            return None

        root = selected_root
        compared_roots.add(root)
        for (component_name, layer), selected_effect in selected_change_map.items():
            reference_effect = reference_change_map[(component_name, layer)]
            if reference_effect <= 0.0 or selected_effect <= 0.0:
                return None
            ratio = selected_effect / reference_effect
            compared_component_count += 1
            if math.isclose(
                ratio, 1.0,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ):
                unchanged_component_count += 1
                continue
            if not math.isfinite(ratio) or ratio <= 0.0:
                return None
            layer_family = _runtime_layer_family(layer)
            for group in groups:
                if layer_family == group['layer_family'] and math.isclose(
                    ratio, group['ratio'],
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    group['ratios'].append(ratio)
                    group['roots'].add(root)
                    group['components'].add(component_name)
                    group['layers'].add(layer)
                    break
            else:
                groups.append({
                    'layer_family': layer_family,
                    'ratio': ratio,
                    'ratios': [ratio],
                    'roots': {root},
                    'components': {component_name},
                    'layers': {layer},
                })

    corroborated = [
        group for group in groups
        if (
            group['ratio'] > 0.0
            and not math.isclose(
                group['ratio'], 1.0,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            )
            and (allow_reduction or group['ratio'] > 1.0)
        )
        and len(compared_roots) >= 2
        and group['roots'] == compared_roots
    ]
    if len(corroborated) != 1:
        return None
    group = corroborated[0]
    if any(candidate is not group for candidate in groups):
        return None
    for identity in reference_actions:
        reference_action = reference_actions[identity]
        selected_action = selected_actions[identity]
        reference_baseline = reference_action.get('baseline')
        selected_baseline = selected_action.get('baseline')
        reference_scenario = (
            _scenario_amounts(reference_action).get(
                scenario_tokens, reference_baseline,
            )
            if reference_scenario_available else reference_baseline
        )
        selected_scenario = _scenario_amounts(selected_action).get(scenario_tokens)
        if selected_scenario is None:
            continue
        if not _scenario_marginal_amount_is_explained(
            reference_baseline, reference_scenario,
            selected_baseline, selected_scenario, group['ratio'],
        ):
            return None
    return {
        'runtime_layer': (
            next(iter(group['layers']))
            if len(group['layers']) == 1
            else group['layer_family']
        ),
        'runtime_components': sorted(group['components']),
        'multiplier': sum(group['ratios']) / len(group['ratios']),
        'evidence_roots': [
            {'token': token, 'spell_id': spell_id}
            for token, spell_id in sorted(
                group['roots'], key=lambda item: (str(item[0]), str(item[1])),
            )
        ],
        'evidence_root_count': len(group['roots']),
        'evidence_component_count': len(group['ratios']),
        'compared_component_count': compared_component_count,
        'unchanged_component_count': unchanged_component_count,
    }


def _scenario_has_target_marginal_change(reference_actor, selected_actor, scenario_tokens):
    action_pair = _global_evidence_action_pair(reference_actor, selected_actor)
    if action_pair is None:
        return True
    reference_actions, selected_actions = action_pair
    for identity, selected_action in selected_actions.items():
        reference_action = reference_actions[identity]
        amounts = (
            reference_action.get('baseline'),
            _scenario_amounts(reference_action).get(
                scenario_tokens, reference_action.get('baseline'),
            ),
            selected_action.get('baseline'),
            _scenario_amounts(selected_action).get(scenario_tokens),
        )
        if amounts[-1] is None:
            continue
        for component_name in ('direct', 'tick'):
            components = tuple((amount or {}).get(component_name) for amount in amounts)
            if all(component is None for component in components):
                continue
            if not all(isinstance(component, dict) for component in components):
                return True
            fields = set().union(*(component.keys() for component in components))
            fields.discard('runtime_layers')
            for field in fields:
                ratio = _scenario_marginal_ratio(
                    components[0].get(field), components[1].get(field),
                    components[2].get(field), components[3].get(field),
                )
                if ratio is None or not math.isclose(
                    ratio, 1.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    return True
            layer_sets = tuple((component.get('runtime_layers') or {}) for component in components)
            if not all(set(layers) == set(layer_sets[0]) for layers in layer_sets):
                return True
            for layer in layer_sets[0]:
                ratio = _scenario_marginal_ratio(
                    layer_sets[0][layer], layer_sets[1][layer],
                    layer_sets[2][layer], layer_sets[3][layer],
                )
                if ratio is None or not math.isclose(
                    ratio, 1.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    return True
    return False


def _selected_scenario_changes_damage(selected_actor, scenario_tokens):
    for action in (_player_skill_actions(selected_actor) or {}).values():
        baseline = action.get('baseline')
        amount = _scenario_amounts(action).get(scenario_tokens)
        if amount is None:
            continue
        layer_changes = _action_runtime_layer_changes(baseline, amount)
        if layer_changes is None or any(
            not math.isclose(
                ratio, 1.0,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            )
            for _component, _layer, ratio in layer_changes
        ):
            return True
        if _effect_changed(baseline, amount):
            return True
    return False


def _runtime_layer_candidates(reference_actor, selected_actor):
    candidates = {}
    passive = _runtime_layer_candidate(reference_actor, selected_actor, ())
    if passive is not None:
        candidates[()] = passive

    selected_tokens = _scenario_token_universe(selected_actor)
    rejected_marginal_tokens = []
    for tokens in selected_tokens:
        selected_only_action_state = _scenario_is_selected_only_action_state(
            reference_actor, selected_actor, tokens,
        )
        candidate = _runtime_layer_scenario_candidate(
            reference_actor, selected_actor, tokens,
            reference_scenario_available=not selected_only_action_state,
        )
        if candidate is not None:
            candidates[tokens] = candidate
        elif _scenario_has_target_marginal_change(
            reference_actor, selected_actor, tokens,
        ):
            rejected_marginal_tokens.append(tokens)
    if rejected_marginal_tokens:
        candidates.pop((), None)
    return candidates


def _talent_declares_all_damage_modifier(talent):
    """Match positive player-wide damage scope within one authoritative clause."""
    if not isinstance(talent, dict):
        return False

    english_patterns = (
        r'\bincreas(?:e|es|ed|ing)\s+(?:the\s+)?(?:all\s+)?damage\s+(?:you(?:\s+both)?\s+deal|dealt\s+by\s+you)(?:\s+to\s+[^,;.]*)?',
        r'\bincreas(?:e|es|ed|ing)\s+your\s+damage(?:\s+dealt)?(?:\s+to\s+[^,;.]*)?',
        r'\byour\s+damage(?:\s+dealt)?\s+(?:is\s+)?increased\b',
        r'\ball\s+damage\s+dealt\s+(?:is\s+)?increased\b',
        r'\b(?:enemies|targets?|they)\b[^.;]*\btake(?:s)?\s+(?:[0-9.]+%\s+)?increased\s+damage\s+from\s+you\b',
    )
    chinese_patterns = (
        r'(?:使)?你(?:和你的宠物)?造成的(?:所有)?伤害(?:会)?提高',
        r'(?:使)?你对(?![^，。；]*(?:施放|技能|法术|攻击))[^，。；]*造成的(?:所有)?伤害(?:会)?提高',
        r'^造成的所有伤害(?:会)?提高',
        r'你的伤害(?:会|将)?提高',
        r'(?:敌人|目标)[^，。；]*受到[^，。；]*来自你的伤害(?:会)?提高',
    )

    for value in (talent.get('description'), talent.get('description_zh')):
        normalized = re.sub(r'\s+', ' ', str(value or '')).lower()
        for clause in re.split(r'[\n.;。；]+', normalized):
            clause = clause.strip()
            if not clause:
                continue
            if re.search(r'\b(?:damage you take|damage dealt by your pet)\b', clause):
                continue
            if re.match(r'^(?:你的)?宠物造成的', clause) or re.search(r'目标[^，。；]*对你造成', clause):
                continue
            if re.search(
                r'\b(?:the\s+)?(?:damage you deal|your damage(?: dealt)?)\s+'
                r'(?:(?:when\s+)?using|with|from)\b',
                clause,
            ):
                continue
            if any(re.search(pattern, clause) for pattern in english_patterns):
                return True
            if any(re.search(pattern, clause) for pattern in chinese_patterns):
                return True
    return False


def classify_global_damage_modifiers(variants):
    """Classify text-declared all-damage effects using cross-skill runtime layers."""
    variants = list(variants or [])
    talent_id_counts = {}
    for item in variants:
        talent_id = (item.get('talent') or {}).get('id') if isinstance(item, dict) else None
        if isinstance(talent_id, int) and not isinstance(talent_id, bool) and talent_id > 0:
            talent_id_counts[talent_id] = talent_id_counts.get(talent_id, 0) + 1

    modifiers = []
    for item in variants:
        talent = item.get('talent') or {}
        talent_id = talent.get('id')
        if talent_id_counts.get(talent_id) != 1:
            continue
        if not _talent_declares_all_damage_modifier(talent):
            continue
        actors = (
            item.get('reference_high'), item.get('high'),
            item.get('reference_low'), item.get('low'),
        )
        if not all(isinstance(actor, dict) for actor in actors):
            continue
        if (
            actors[0].get('talent_effectiveness') != 'inactive'
            or actors[1].get('talent_effectiveness') != 'active'
            or actors[2].get('talent_effectiveness') != 'inactive'
            or actors[3].get('talent_effectiveness') != 'active'
        ):
            continue

        high_candidates = _runtime_layer_candidates(actors[0], actors[1])
        low_candidates = _runtime_layer_candidates(actors[2], actors[3])
        common = []
        for scenario_tokens in set(high_candidates) & set(low_candidates):
            high = high_candidates[scenario_tokens]
            low = low_candidates[scenario_tokens]
            if (
                high.get('runtime_layer') == low.get('runtime_layer')
                and high.get('runtime_components') == low.get('runtime_components')
                and math.isclose(
                    high['multiplier'], low['multiplier'],
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                )
            ):
                common.append((scenario_tokens, high, low))
        for scenario_tokens, high, low in sorted(common, key=lambda candidate: candidate[0]):
            if not scenario_tokens and any(
                set(_player_skill_actions(actors[reference_index]))
                != set(_player_skill_actions(actors[selected_index]))
                for reference_index, selected_index in ((0, 1), (2, 3))
            ):
                continue
            multiplier = (high['multiplier'] + low['multiplier']) / 2.0
            runtime_condition = ''
            if scenario_tokens:
                runtime_condition = f'探针条件：启用 {" + ".join(scenario_tokens)} buff'
            modifiers.append({
                'talent_id': talent_id,
                'talent_name': str(talent.get('name') or ''),
                'talent_name_zh': str(talent.get('name_zh') or ''),
                'tree_type': str(talent.get('tree_type') or ''),
                'hero_subtree_id': talent.get('hero_subtree_id'),
                'hero_subtree_name': str(talent.get('hero_subtree_name') or ''),
                'hero_subtree_name_zh': str(talent.get('hero_subtree_name_zh') or ''),
                'damage_multiplier': multiplier,
                'damage_bonus_percent': (multiplier - 1.0) * 100.0,
                'runtime_condition': runtime_condition,
                'scenario_tokens': list(scenario_tokens),
                'runtime_layer': high['runtime_layer'],
                'runtime_components': list(high['runtime_components']),
                'evidence_roots': list(high['evidence_roots']),
                'scope': 'declared_all_damage_runtime_layer_correlated',
                'evidence_root_count': min(
                    high['evidence_root_count'], low['evidence_root_count'],
                ),
                'evidence_component_count': min(
                    high['evidence_component_count'], low['evidence_component_count'],
                ),
                'compared_component_count': min(
                    high['compared_component_count'], low['compared_component_count'],
                ),
                'unchanged_component_count': max(
                    high['unchanged_component_count'], low['unchanged_component_count'],
                ),
            })
    return modifiers


def _scenario_metadata(actor, scenario_tokens):
    matched = []
    for action in ((actor or {}).get('actions') or []):
        for scenario in (action.get('scenarios') or []) if isinstance(action, dict) else []:
            if not isinstance(scenario, dict) or _scenario_tokens(scenario) != tuple(scenario_tokens):
                continue
            buffs = scenario.get('buffs') or []
            if not isinstance(buffs, list):
                continue
            matched.extend(buff for buff in buffs if isinstance(buff, dict))
    result = []
    seen = set()
    for buff in matched:
        identity = (
            str(buff.get('token') or '').strip(),
            buff.get('spell_id'),
            str(buff.get('scope') or '').strip(),
        )
        if not identity[0] or identity in seen:
            continue
        seen.add(identity)
        result.append({
            'token': identity[0],
            'spell_id': identity[1] if isinstance(identity[1], int) else 0,
            'scope': identity[2],
        })
    return result


def _neutralize_actor_scenario(actor, scenario_tokens):
    neutral = copy.deepcopy(actor or {})
    for action in neutral.get('actions') or []:
        if not isinstance(action, dict):
            continue
        baseline = action.get('baseline')
        for scenario in action.get('scenarios') or []:
            if not isinstance(scenario, dict) or _scenario_tokens(scenario) != tuple(scenario_tokens):
                continue
            field = 'values' if 'values' in scenario else 'amount'
            scenario[field] = copy.deepcopy(baseline)
    return neutral


def _uniform_crit_scenario_candidate(actor, scenario_tokens, *, damage_multiplier=1.0):
    actions = _player_skill_actions(actor)
    if not actions or not _finite_number(damage_multiplier) or damage_multiplier <= 0.0:
        return None
    deltas = []
    roots = set()
    components = set()
    for action in actions.values():
        scenario = _scenario_amounts(action).get(tuple(scenario_tokens))
        if scenario is None:
            continue
        root = (
            action.get('reporting_root_token'),
            action.get('reporting_root_spell_id'),
        )
        root_compared = False
        for component_name in ('direct', 'tick'):
            baseline_component = (action.get('baseline') or {}).get(component_name)
            scenario_component = (scenario or {}).get(component_name)
            if baseline_component is None and scenario_component is None:
                continue
            if not isinstance(baseline_component, dict) or not isinstance(scenario_component, dict):
                return None
            baseline_can_crit = baseline_component.get('can_crit')
            scenario_can_crit = scenario_component.get('can_crit')
            if baseline_can_crit is not True or scenario_can_crit is not True:
                if baseline_can_crit is not False or scenario_can_crit is not False:
                    return None
                for field in (
                    'crit_chance', 'crit_chance_uncapped', 'crit_multiplier',
                    'damage_equivalent_count',
                ):
                    if not _fact_equal(
                        baseline_component.get(field), scenario_component.get(field),
                    ):
                        return None
                for field in ('hit', 'crit', 'expected'):
                    baseline_value = baseline_component.get(field)
                    scenario_value = scenario_component.get(field)
                    if not _finite_number(baseline_value) or not _finite_number(scenario_value):
                        return None
                    if not math.isclose(
                        scenario_value, baseline_value * damage_multiplier,
                        rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                        abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    ):
                        return None
                if math.isclose(
                    damage_multiplier, 1.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ) and baseline_component.get('runtime_layers') != scenario_component.get('runtime_layers'):
                    return None
                if baseline_component.get('base_damage_layers') != scenario_component.get('base_damage_layers'):
                    return None
                continue
            baseline_chance = baseline_component.get('crit_chance_uncapped')
            scenario_chance = scenario_component.get('crit_chance_uncapped')
            if not _finite_number(baseline_chance) or not _finite_number(scenario_chance):
                return None
            delta = scenario_chance - baseline_chance
            if math.isclose(
                delta, 0.0,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ):
                return None
            for field in ('crit_multiplier', 'damage_equivalent_count'):
                if not _fact_equal(baseline_component.get(field), scenario_component.get(field)):
                    return None
            for field in ('hit', 'crit'):
                baseline_value = baseline_component.get(field)
                scenario_value = scenario_component.get(field)
                if not _finite_number(baseline_value) or not _finite_number(scenario_value):
                    return None
                if not math.isclose(
                    scenario_value, baseline_value * damage_multiplier,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    return None
            if math.isclose(
                damage_multiplier, 1.0,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ) and baseline_component.get('runtime_layers') != scenario_component.get('runtime_layers'):
                return None
            if baseline_component.get('base_damage_layers') != scenario_component.get('base_damage_layers'):
                return None
            actual_chance = scenario_component.get('crit_chance')
            expected = scenario_component.get('expected')
            if not all(_finite_number(value) for value in (
                actual_chance, expected,
                scenario_component.get('hit'), scenario_component.get('crit'),
            )):
                return None
            calculated = (
                scenario_component['hit'] * (1.0 - actual_chance)
                + scenario_component['crit'] * actual_chance
            )
            if not math.isclose(
                expected, calculated,
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            ):
                return None
            deltas.append(delta)
            components.add(component_name)
            root_compared = True
        if root_compared:
            roots.add(root)
    if len(roots) < 2 or not deltas:
        return None
    average = sum(deltas) / len(deltas)
    if any(not math.isclose(
        delta, average,
        rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
        abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
    ) for delta in deltas):
        return None
    return {
        'chance_delta': average,
        'runtime_components': sorted(components),
        'evidence_roots': [
            {'token': token, 'spell_id': spell_id}
            for token, spell_id in sorted(roots, key=lambda item: (str(item[0]), str(item[1])))
        ],
        'evidence_root_count': len(roots),
        'evidence_component_count': len(deltas),
    }


def _base_damage_layer_candidate(reference_actor, selected_actor):
    reference_player_actions = _player_skill_actions(reference_actor)
    selected_player_actions = _player_skill_actions(selected_actor)
    if not reference_player_actions or set(reference_player_actions) != set(selected_player_actions):
        return None
    action_pair = _global_evidence_action_pair(reference_actor, selected_actor)
    if action_pair is None:
        return None
    reference_actions, selected_actions = action_pair
    groups = {}
    mirrored_runtime_groups = {}
    compared_roots = set()
    comparable_components = set()
    component_count = 0
    for identity, reference_action in reference_actions.items():
        selected_action = selected_actions[identity]
        reference_root = (
            reference_action.get('reporting_root_token'),
            reference_action.get('reporting_root_spell_id'),
        )
        selected_root = (
            selected_action.get('reporting_root_token'),
            selected_action.get('reporting_root_spell_id'),
        )
        if reference_root != selected_root or (not selected_root[0] and not selected_root[1]):
            return None
        root_compared = False
        for component_name in ('direct', 'tick'):
            reference_component = (reference_action.get('baseline') or {}).get(component_name)
            selected_component = (selected_action.get('baseline') or {}).get(component_name)
            if reference_component is None and selected_component is None:
                continue
            if not isinstance(reference_component, dict) or not isinstance(selected_component, dict):
                return None
            reference_layers = reference_component.get('base_damage_layers')
            selected_layers = selected_component.get('base_damage_layers')
            if not isinstance(reference_layers, dict) or not isinstance(selected_layers, dict):
                return None
            if set(reference_layers) != {'base_multiplier', 'component_multiplier'} or set(selected_layers) != set(reference_layers):
                return None
            component_ratio = 1.0
            changed_layers = []
            for layer in ('base_multiplier', 'component_multiplier'):
                reference_value = reference_layers.get(layer)
                selected_value = selected_layers.get(layer)
                if not _finite_number(reference_value) or not _finite_number(selected_value) or reference_value <= 0 or selected_value <= 0:
                    return None
                ratio = selected_value / reference_value
                if not math.isclose(
                    ratio, 1.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    component_ratio *= ratio
                    changed_layers.append((layer, ratio))
            component_identity = (selected_root, component_name)
            comparable_components.add(component_identity)
            if not changed_layers:
                if not _fact_equal(reference_component, selected_component):
                    return None
                continue
            reference_runtime_layers = reference_component.get('runtime_layers')
            selected_runtime_layers = selected_component.get('runtime_layers')
            if (
                not isinstance(reference_runtime_layers, dict)
                or not isinstance(selected_runtime_layers, dict)
                or set(reference_runtime_layers) != set(selected_runtime_layers)
            ):
                return None
            mirrored_runtime_changes = []
            for layer, reference_value in reference_runtime_layers.items():
                selected_value = selected_runtime_layers.get(layer)
                if (
                    not _finite_number(reference_value)
                    or not _finite_number(selected_value)
                    or reference_value <= 0
                    or selected_value <= 0
                ):
                    return None
                ratio = selected_value / reference_value
                if not math.isclose(
                    ratio, 1.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    mirrored_runtime_changes.append((layer, ratio))
            if mirrored_runtime_changes and (
                len(mirrored_runtime_changes) != 1
                or not math.isclose(
                    mirrored_runtime_changes[0][1], component_ratio,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                )
            ):
                return None
            for field in (
                'crit_multiplier', 'crit_chance', 'crit_chance_uncapped',
                'can_crit', 'damage_equivalent_count',
            ):
                if not _fact_equal(reference_component.get(field), selected_component.get(field)):
                    return None
            for field in ('hit', 'crit', 'expected'):
                reference_value = reference_component.get(field)
                selected_value = selected_component.get(field)
                if not _finite_number(reference_value) or not _finite_number(selected_value):
                    return None
                if math.isclose(reference_value, 0.0, abs_tol=1.0e-12):
                    if not math.isclose(selected_value, 0.0, abs_tol=1.0e-12):
                        return None
                elif not math.isclose(
                    selected_value / reference_value, component_ratio,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    return None
            for layer, ratio in changed_layers:
                group = groups.setdefault(
                    layer,
                    {'ratios': [], 'roots': set(), 'components': set(), 'evidence': set()},
                )
                group['ratios'].append(ratio)
                group['roots'].add(selected_root)
                group['components'].add(component_name)
                group['evidence'].add(component_identity)
            for layer, ratio in mirrored_runtime_changes:
                group = mirrored_runtime_groups.setdefault(
                    layer,
                    {'ratios': [], 'roots': set(), 'components': set(), 'evidence': set()},
                )
                group['ratios'].append(ratio)
                group['roots'].add(selected_root)
                group['components'].add(component_name)
                group['evidence'].add(component_identity)
            component_count += 1
            root_compared = True
        if root_compared:
            compared_roots.add(selected_root)
    if len(compared_roots) < 2 or not groups:
        return None
    if any(group['evidence'] != comparable_components for group in groups.values()):
        return None
    if mirrored_runtime_groups and set().union(*(
        group['evidence'] for group in mirrored_runtime_groups.values()
    )) != comparable_components:
        return None
    multiplier = 1.0
    for group in groups.values():
        average = sum(group['ratios']) / len(group['ratios'])
        if any(not math.isclose(
            ratio, average,
            rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
        ) for ratio in group['ratios']):
            return None
        multiplier *= average
    if multiplier <= 1.0 + _GLOBAL_DAMAGE_RATIO_REL_TOLERANCE:
        return None
    for group in mirrored_runtime_groups.values():
        if any(not math.isclose(
            ratio, multiplier,
            rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
        ) for ratio in group['ratios']):
            return None
    return {
        'multiplier': multiplier,
        'runtime_layer': '+'.join(f'base_damage.{layer}' for layer in sorted(groups)),
        'runtime_components': sorted(set().union(*(group['components'] for group in groups.values()))),
        'evidence_roots': [
            {'token': token, 'spell_id': spell_id}
            for token, spell_id in sorted(compared_roots, key=lambda item: (str(item[0]), str(item[1])))
        ],
        'evidence_root_count': len(compared_roots),
        'evidence_component_count': component_count,
        'mirrored_runtime_layers': sorted(mirrored_runtime_groups),
    }


def _global_effect_identity(prefix, scenario_tokens=()):
    suffix = '+'.join(scenario_tokens) if scenario_tokens else 'passive'
    return f'{prefix}:{suffix}'


def _amount_change_only_global_multiplier(reference, selected, multiplier):
    if (
        _amount_state(reference)[0] != 'resolved'
        or _amount_state(selected)[0] != 'resolved'
        or not _finite_number(multiplier)
        or multiplier <= 0
    ):
        return False
    changed = False
    scalable_fields = {'hit', 'crit', 'expected'}
    for component_name in ('direct', 'tick'):
        reference_component = reference.get(component_name)
        selected_component = selected.get(component_name)
        if reference_component is None and selected_component is None:
            continue
        if not isinstance(reference_component, dict) or not isinstance(selected_component, dict):
            return False
        for field in set(reference_component) | set(selected_component):
            reference_value = reference_component.get(field)
            selected_value = selected_component.get(field)
            values = None
            if field in scalable_fields:
                values = ((reference_value, selected_value),)
            elif field in ('base_damage_layers', 'runtime_layers'):
                if not isinstance(reference_value, dict) or not isinstance(selected_value, dict):
                    return False
                if set(reference_value) != set(selected_value):
                    return False
                values = tuple(
                    (reference_value[key], selected_value[key]) for key in reference_value
                )
            if values is None:
                if not _fact_equal(reference_value, selected_value):
                    return False
                continue
            for old, new in values:
                if not _finite_number(old) or not _finite_number(new) or old <= 0:
                    if not _fact_equal(old, new):
                        return False
                    continue
                ratio = new / old
                if math.isclose(
                    ratio, 1.0,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    continue
                if not math.isclose(
                    ratio, multiplier,
                    rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                    abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                ):
                    return False
                changed = True
    return changed and _fact_equal(
        reference.get('unresolved_reason'), selected.get('unresolved_reason'),
    )


def classify_global_skill_effects(base_high, base_low, variants):
    effects = []

    def append_effect(*, effect_id, source_type, scenario_tokens, projections, source=None, evidence=None):
        row = {
            'effect_id': effect_id,
            'source_type': source_type,
            'scenario_tokens': list(scenario_tokens),
            'runtime_condition': (
                f'启用 {" + ".join(scenario_tokens)}' if scenario_tokens else ''
            ),
            'projections': projections,
        }
        row.update(copy.deepcopy(source or {}))
        row['evidence'] = copy.deepcopy(evidence or {})
        effects.append(row)

    high_tokens = _scenario_token_universe(base_high)
    low_tokens = _scenario_token_universe(base_low)
    for scenario_tokens in sorted(high_tokens & low_tokens):
        if len(scenario_tokens) != 1:
            continue
        neutral_high = _neutralize_actor_scenario(base_high, scenario_tokens)
        neutral_low = _neutralize_actor_scenario(base_low, scenario_tokens)
        high_damage = _runtime_layer_scenario_candidate(
            neutral_high, base_high, scenario_tokens, allow_reduction=True,
        )
        low_damage = _runtime_layer_scenario_candidate(
            neutral_low, base_low, scenario_tokens, allow_reduction=True,
        )
        high_crit = _uniform_crit_scenario_candidate(
            base_high, scenario_tokens,
            damage_multiplier=(high_damage or {}).get('multiplier', 1.0),
        )
        low_crit = _uniform_crit_scenario_candidate(
            base_low, scenario_tokens,
            damage_multiplier=(low_damage or {}).get('multiplier', 1.0),
        )
        projections = []
        evidence = {}
        if high_damage and low_damage and (
            high_damage.get('runtime_layer') == low_damage.get('runtime_layer')
            and high_damage.get('runtime_components') == low_damage.get('runtime_components')
            and math.isclose(
                high_damage['multiplier'], low_damage['multiplier'],
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            )
        ):
            multiplier = (high_damage['multiplier'] + low_damage['multiplier']) / 2.0
            projections.append({
                'kind': 'damage_multiplier', 'operation': 'multiply',
                'value': multiplier, 'bonus_percent': (multiplier - 1.0) * 100.0,
                'evidence_layer': high_damage['runtime_layer'],
            })
            evidence['damage'] = high_damage
        if high_crit and low_crit and math.isclose(
            high_crit['chance_delta'], low_crit['chance_delta'],
            rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
        ):
            delta = (high_crit['chance_delta'] + low_crit['chance_delta']) / 2.0
            projections.append({
                'kind': 'crit_chance', 'operation': 'add',
                'value': delta, 'percentage_points': delta * 100.0,
                'evidence_layer': 'crit_chance_uncapped',
            })
            evidence['crit'] = high_crit
        if projections:
            metadata = _scenario_metadata(base_high, scenario_tokens)
            append_effect(
                effect_id=_global_effect_identity('runtime_state', scenario_tokens),
                source_type='runtime_state', scenario_tokens=scenario_tokens,
                projections=projections,
                source={
                    'source_spell_ids': sorted({
                        item['spell_id'] for item in metadata if item.get('spell_id')
                    }),
                    'source_token': scenario_tokens[0],
                },
                evidence=evidence,
            )

    talent_damage_modifiers = {}
    for modifier in classify_global_damage_modifiers(variants):
        talent_damage_modifiers.setdefault(modifier['talent_id'], []).append(modifier)
    talent_id_counts = {}
    for item in variants or []:
        talent_id = (item.get('talent') or {}).get('id')
        if isinstance(talent_id, int) and not isinstance(talent_id, bool) and talent_id > 0:
            talent_id_counts[talent_id] = talent_id_counts.get(talent_id, 0) + 1
    for item in variants or []:
        talent = item.get('talent') or {}
        talent_id = talent.get('id')
        if talent_id_counts.get(talent_id) != 1 or not _talent_declares_all_damage_modifier(talent):
            continue
        source = {
            'talent_id': talent_id,
            'talent_name': str(talent.get('name') or ''),
            'talent_name_zh': str(talent.get('name_zh') or ''),
            'tree_type': str(talent.get('tree_type') or ''),
            'hero_subtree_id': talent.get('hero_subtree_id'),
            'hero_subtree_name': str(talent.get('hero_subtree_name') or ''),
            'hero_subtree_name_zh': str(talent.get('hero_subtree_name_zh') or ''),
        }
        damages = talent_damage_modifiers.get(talent_id) or []
        if damages:
            for damage in sorted(
                damages, key=lambda row: tuple(row.get('scenario_tokens') or []),
            ):
                multiplier = damage['damage_multiplier']
                append_effect(
                    effect_id=_global_effect_identity(
                        f'talent:{talent_id}', tuple(damage.get('scenario_tokens') or []),
                    ),
                    source_type='talent',
                    scenario_tokens=tuple(damage.get('scenario_tokens') or []),
                    projections=[{
                        'kind': 'damage_multiplier', 'operation': 'multiply',
                        'value': multiplier, 'bonus_percent': (multiplier - 1.0) * 100.0,
                        'evidence_layer': damage.get('runtime_layer'),
                    }],
                    source=source, evidence={'damage': damage},
                )
            continue
        actors = (
            item.get('reference_high'), item.get('high'),
            item.get('reference_low'), item.get('low'),
        )
        if not all(isinstance(actor, dict) for actor in actors) or (
            actors[0].get('talent_effectiveness') != 'inactive'
            or actors[1].get('talent_effectiveness') != 'active'
            or actors[2].get('talent_effectiveness') != 'inactive'
            or actors[3].get('talent_effectiveness') != 'active'
        ):
            continue
        high_base = _base_damage_layer_candidate(actors[0], actors[1])
        low_base = _base_damage_layer_candidate(actors[2], actors[3])
        if not high_base or not low_base or (
            high_base.get('runtime_layer') != low_base.get('runtime_layer')
            or high_base.get('runtime_components') != low_base.get('runtime_components')
            or not math.isclose(
                high_base['multiplier'], low_base['multiplier'],
                rel_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
                abs_tol=_GLOBAL_DAMAGE_RATIO_REL_TOLERANCE,
            )
        ):
            continue
        multiplier = (high_base['multiplier'] + low_base['multiplier']) / 2.0
        append_effect(
            effect_id=_global_effect_identity(f'talent:{talent_id}'),
            source_type='talent', scenario_tokens=(),
            projections=[{
                'kind': 'damage_multiplier', 'operation': 'multiply',
                'value': multiplier, 'bonus_percent': (multiplier - 1.0) * 100.0,
                'evidence_layer': high_base['runtime_layer'],
            }],
            source=source, evidence={'base_damage': high_base},
        )
    return effects


def flatten_single_talent_damage_variants(base_high, base_low, variants, *, global_effects=None):
    """Flatten every independently exported SimC fact without recalculating damage."""
    base_high_actions = {
        _action_identity(action): action
        for action in (base_high.get('actions') or [])
        if isinstance(action, dict) and action.get('supported') is True
    }
    base_low_actions = {
        _action_identity(action): action
        for action in (base_low.get('actions') or [])
        if isinstance(action, dict) and action.get('supported') is True
    }
    rows = []
    if global_effects is None:
        classified_global_modifiers = classify_global_damage_modifiers(variants)
        passive_global_talent_ids = {
            modifier.get('talent_id')
            for modifier in classified_global_modifiers
            if not modifier.get('scenario_tokens')
        }
        passive_global_multipliers = {
            modifier.get('talent_id'): modifier.get('damage_multiplier')
            for modifier in classified_global_modifiers
            if not modifier.get('scenario_tokens')
        }
        global_scenario_token_sets = {
            tuple(modifier.get('scenario_tokens') or [])
            for modifier in classified_global_modifiers
            if modifier.get('scenario_tokens')
        }
    else:
        passive_global_talent_ids = {
            effect.get('talent_id')
            for effect in global_effects
            if effect.get('source_type') == 'talent'
            and effect.get('talent_id') is not None
            and not effect.get('scenario_tokens')
        }
        passive_global_multipliers = {
            effect.get('talent_id'): projection.get('value')
            for effect in global_effects
            if effect.get('source_type') == 'talent'
            and not effect.get('scenario_tokens')
            for projection in (effect.get('projections') or [])
            if projection.get('kind') == 'damage_multiplier'
        }
        global_scenario_token_sets = {
            tuple(effect.get('scenario_tokens') or [])
            for effect in global_effects
            if effect.get('scenario_tokens')
        }

    def includes_global_scenario(tokens):
        token_set = set(tokens or ())
        return any(set(global_tokens).issubset(token_set) for global_tokens in global_scenario_token_sets)

    def append_row(action, amount, *, talent, condition, comparison, scenario_tokens=()):
        if not isinstance(action, dict) or _amount_state(amount)[0] != 'resolved':
            return
        talent_hero_subtree_id = talent.get('hero_subtree_id')
        hero_subtree_ids = []
        if (
            talent.get('tree_type') == 'hero'
            and isinstance(talent_hero_subtree_id, int)
            and not isinstance(talent_hero_subtree_id, bool)
            and talent_hero_subtree_id > 0
        ):
            hero_subtree_ids = [talent_hero_subtree_id]
        row = copy.deepcopy(action)
        if hero_subtree_ids:
            row['hero_subtree_ids'] = hero_subtree_ids
        row['baseline'] = copy.deepcopy(amount)
        row['scenarios'] = []
        reference_state = _amount_state(comparison)
        row['variant'] = {
            'talent_id': talent.get('id'),
            'talent_name': str(talent.get('name') or ''),
            'talent_name_zh': str(talent.get('name_zh') or ''),
            'tree_type': str(talent.get('tree_type') or ''),
            'hero_subtree_id': talent.get('hero_subtree_id'),
            'hero_subtree_name': str(talent.get('hero_subtree_name') or ''),
            'hero_subtree_name_zh': str(talent.get('hero_subtree_name_zh') or ''),
            'trait_entry_id': talent.get('node_id'),
            'runtime_condition': condition,
            'scenario_tokens': list(scenario_tokens),
            'reference_available': reference_state[0] == 'resolved',
        }
        if reference_state[0] == 'absent':
            row['variant']['reference_unavailable_reason'] = 'action_absent_in_reference_actor'
        elif reference_state[0] == 'unresolved':
            row['variant']['reference_unavailable_reason'] = (
                f'reference_runtime_unresolved:{reference_state[1]}'
            )
        rows.append(row)

    no_talent = {'id': None, 'name': '', 'name_zh': '', 'tree_type': '', 'node_id': None}
    for identity in dict.fromkeys([*base_high_actions, *base_low_actions]):
        high_action = base_high_actions.get(identity)
        low_action = base_low_actions.get(identity)
        high_amount = high_action.get('baseline') if high_action else None
        low_amount = low_action.get('baseline') if low_action else None
        if _amount_state(high_amount)[0] == 'resolved':
            append_row(
                high_action, high_amount, talent=no_talent,
                condition='', comparison=high_amount,
            )
        if _amount_state(low_amount)[0] == 'resolved' and (
            _amount_state(high_amount)[0] != 'resolved' or _effect_changed(high_amount, low_amount)
        ):
            append_row(
                low_action, low_amount, talent=no_talent,
                condition='目标生命值低于 35%',
                comparison=low_amount,
            )

    for item in variants:
        talent = item.get('talent') or {}
        passive_global_talent = talent.get('id') in passive_global_talent_ids
        reference_high_actions = {
            _action_identity(action): action
            for action in ((item.get('reference_high') or {}).get('actions') or [])
            if isinstance(action, dict) and action.get('supported') is True
        }
        reference_low_actions = {
            _action_identity(action): action
            for action in ((item.get('reference_low') or {}).get('actions') or [])
            if isinstance(action, dict) and action.get('supported') is True
        }
        high_actions = {
            _action_identity(action): action
            for action in ((item.get('high') or {}).get('actions') or [])
            if isinstance(action, dict) and action.get('supported') is True
        }
        low_actions = {
            _action_identity(action): action
            for action in ((item.get('low') or {}).get('actions') or [])
            if isinstance(action, dict) and action.get('supported') is True
        }
        for identity in dict.fromkeys([*high_actions, *low_actions]):
            high_action = high_actions.get(identity)
            low_action = low_actions.get(identity)
            if passive_global_talent and any(
                action and action.get('player_skill') is True
                for action in (high_action, low_action)
            ):
                continue
            base_high_action = reference_high_actions.get(identity)
            base_low_action = reference_low_actions.get(identity)
            high_amount = high_action.get('baseline') if high_action else None
            low_amount = low_action.get('baseline') if low_action else None
            base_high_amount = base_high_action.get('baseline') if base_high_action else None
            base_low_amount = base_low_action.get('baseline') if base_low_action else None
            candidates = []

            if (
                _amount_state(high_amount)[0] == 'resolved'
                and _effect_changed(base_high_amount, high_amount)
            ):
                candidates.append((
                    high_action, high_amount, base_high_amount,
                    '', (),
                ))

            base_high_scenarios = _scenario_amounts(base_high_action)
            high_scenarios = _scenario_amounts(high_action)
            base_low_scenarios = _scenario_amounts(base_low_action)
            low_scenarios = _scenario_amounts(low_action)

            for tokens, amount in high_scenarios.items():
                if includes_global_scenario(tokens):
                    continue
                reference = base_high_scenarios.get(tokens, base_high_amount)
                if _amount_state(amount)[0] == 'resolved' and _effect_changed(reference, amount):
                    candidates.append((
                        high_action, amount, reference,
                        f'探针条件：启用 {" + ".join(tokens)} buff', tokens,
                    ))

            if (
                _amount_state(low_amount)[0] == 'resolved'
                and _paired_effect_changed(
                    base_high_amount, high_amount, base_low_amount, low_amount,
                )
            ):
                candidates.append((
                    low_action, low_amount, base_low_amount,
                    '目标生命值低于 35%', (),
                ))

            for tokens, amount in low_scenarios.items():
                if includes_global_scenario(tokens):
                    continue
                low_reference = base_low_scenarios.get(tokens, base_low_amount)
                high_current = high_scenarios.get(tokens, high_amount)
                high_reference = base_high_scenarios.get(tokens, base_high_amount)
                if _amount_state(amount)[0] == 'resolved' and _paired_effect_changed(
                    high_reference, high_current, low_reference, amount,
                ):
                    candidates.append((
                        low_action, amount, low_reference,
                        f'目标生命值低于 35% + 探针启用 {" + ".join(tokens)} buff', tokens,
                    ))

            seen_candidates = set()
            for source_action, amount, comparison, condition, tokens in candidates:
                if (
                    passive_global_talent
                    and source_action.get('player_skill') is not True
                    and _amount_change_only_global_multiplier(
                        comparison, amount,
                        passive_global_multipliers.get(talent.get('id')),
                    )
                ):
                    continue
                identity_key = (
                    condition,
                    json.dumps(amount, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
                )
                if identity_key in seen_candidates:
                    continue
                seen_candidates.add(identity_key)
                append_row(
                    source_action, amount, talent=talent, condition=condition,
                    comparison=comparison, scenario_tokens=tokens,
                )
    attach_runtime_product_metrics({'actions': rows})
    return rows


def attach_runtime_product_metrics(actor):
    """Combine raw DBC scaling with one fully-talented SimC runtime actor.

    DBC coefficients are the pre-talent spell fact. Runtime hit/crit/expectation
    already include the selected build through SimC's native action formulas.
    """
    for action in actor.get('actions') or []:
        if not isinstance(action, dict) or action.get('supported') is not True:
            continue
        baseline = action.get('baseline')
        if not isinstance(baseline, dict) or baseline.get('unresolved_reason'):
            continue
        dbc_scaling = action.get('dbc_scaling') or {}
        for component_name in ('direct', 'tick'):
            component = baseline.get(component_name)
            if not isinstance(component, dict):
                continue
            dbc_component = dbc_scaling.get(component_name)
            dbc_value = (
                dbc_component.get('normalized_base')
                if isinstance(dbc_component, dict)
                else None
            )
            dbc_reason = ''
            if not _finite_number(dbc_value):
                dbc_value = None
                dbc_reason = 'dbc_damage_effect_unresolved'
            component['product'] = {
                'dbc_base_damage_min': dbc_value,
                'dbc_base_damage_max': dbc_value,
                'current_talent_damage': component.get('hit'),
                'crit_damage': component.get('crit'),
                'crit_multiplier': component.get('crit_multiplier'),
                'actual_crit_chance': component.get('crit_chance'),
                'normalized_expected': component.get('expected'),
                'dbc_unresolved_reason': dbc_reason,
            }
    return actor


def project_skill_damage_product_payload(payload):
    """Return one display row per proven SimC reporting-root cast."""
    result = copy.deepcopy(payload or {})
    display_count = 0
    required = (
        'current_talent_damage', 'crit_damage', 'crit_multiplier',
        'actual_crit_chance', 'normalized_expected',
    )
    actors = [actor for actor in (result.get('actors') or []) if isinstance(actor, dict)]
    for actor in actors:
        groups = {}
        hand_groups = {}
        for action in actor.get('actions') or []:
            if not isinstance(action, dict) or action.get('supported') is not True:
                continue
            baseline = action.get('baseline')
            if not isinstance(baseline, dict) or baseline.get('unresolved_reason'):
                continue
            base_token, hand = _action_hand_component_identity(action)
            root_token = _text_key(action.get('reporting_root_token'))
            if (
                not base_token or not hand
                or action.get('reporting_root_component') is not True
                or root_token not in {
                    _text_key(action.get('token')), _text_key(action.get('name')),
                }
            ):
                continue
            variant_key, hero_subtree_ids = _action_variant_ownership_key(action)
            hand_key = (base_token, variant_key, hero_subtree_ids)
            hand_group = hand_groups.setdefault(hand_key, {'hands': set(), 'spell_ids': set()})
            hand_group['hands'].add(hand)
            root_spell_id = action.get('reporting_root_spell_id')
            if isinstance(root_spell_id, int) and not isinstance(root_spell_id, bool):
                hand_group['spell_ids'].add(root_spell_id)
        paired_hand_groups = {
            key: value for key, value in hand_groups.items()
            if value['hands'] == {'main', 'off'}
        }

        for action in actor.get('actions') or []:
            if not isinstance(action, dict) or action.get('supported') is not True:
                continue
            baseline = action.get('baseline')
            if not isinstance(baseline, dict) or baseline.get('unresolved_reason'):
                continue
            for component_name in ('direct', 'tick'):
                component = baseline.get(component_name)
                product = component.get('product') if isinstance(component, dict) else None
                if not isinstance(product, dict):
                    continue
                if any(not _finite_number(product.get(field)) for field in required):
                    continue
                chance = product['actual_crit_chance']
                if not 0.0 <= chance <= 1.0 or product['crit_multiplier'] < 0.0:
                    continue
                count = component.get('damage_equivalent_count', 1.0)
                if not _finite_number(count) or count <= 0:
                    continue
                dbc_scaling = action.get('dbc_scaling') or {}
                dbc_component = dbc_scaling.get(component_name)
                if not isinstance(dbc_component, dict):
                    continue
                normalized_base = product.get('dbc_base_damage_min')
                normalized_max = product.get('dbc_base_damage_max')
                final_damage = product.get('current_talent_damage')
                ap_coeff = dbc_component.get('attack_power_coefficient')
                sp_coeff = dbc_component.get('spell_power_coefficient')
                if not (
                    _finite_number(normalized_base) and _finite_number(normalized_max)
                    and normalized_base == normalized_max and _finite_number(final_damage)
                    and _finite_number(ap_coeff) and _finite_number(sp_coeff)
                ):
                    continue
                root_component = action.get('reporting_root_component') is True
                root_token = action.get('reporting_root_token') if root_component else action.get('token')
                root_spell_id = action.get('reporting_root_spell_id') if root_component else action.get('spell_id')
                variant_key, hero_subtree_ids = _action_variant_ownership_key(action)
                base_token, hand = _action_hand_component_identity(action)
                hand_key = (base_token, variant_key, hero_subtree_ids)
                self_root = _text_key(root_token) in {
                    _text_key(action.get('token')), _text_key(action.get('name')),
                }
                paired_hand_group = paired_hand_groups.get(hand_key)
                if root_component and hand and self_root and paired_hand_group:
                    root_token = base_token
                    root_spell_ids = paired_hand_group['spell_ids']
                    root_spell_id = next(iter(root_spell_ids)) if len(root_spell_ids) == 1 else None
                group_key = (
                    str(root_token or ''), root_spell_id,
                    variant_key,
                    hero_subtree_ids,
                )
                group = groups.get(group_key)
                if group is None:
                    row = {
                        key: copy.deepcopy(value)
                        for key, value in action.items()
                        if key not in ('baseline', 'scenarios', 'unsupported_reason', 'dbc_scaling')
                    }
                    row['token'] = root_token
                    row['spell_id'] = root_spell_id
                    row['component'] = 'combined'
                    if hero_subtree_ids:
                        row['hero_subtree_ids'] = list(hero_subtree_ids)
                    else:
                        row.pop('hero_subtree_ids', None)
                    row['component_count'] = 0
                    row['components'] = []
                    row['product'] = {
                        'attack_power_coefficient': 0.0,
                        'spell_power_coefficient': 0.0,
                        'normalized_base_damage': 0.0,
                        'final_normalized_damage': 0.0,
                        'formula_components': [],
                    }
                    group = groups[group_key] = row
                weighted_base = normalized_base * count
                weighted_final = final_damage * count
                runtime_layers = component.get('runtime_layers') or {}
                runtime_factors = [
                    value
                    for value in (
                        runtime_layers.values() if isinstance(runtime_layers, dict) else ()
                    )
                    if _finite_number(value)
                    and not math.isclose(value, 1.0, rel_tol=0.0, abs_tol=1e-12)
                ]
                runtime_product = math.prod(runtime_factors)
                formula_base = weighted_base
                if not math.isclose(
                    weighted_base * runtime_product,
                    weighted_final,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                ):
                    formula_base = weighted_final / runtime_product
                group['component_count'] += 1
                group['components'].append({
                    'token': action.get('token'), 'spell_id': action.get('spell_id'),
                    'component': component_name, 'damage_equivalent_count': count,
                    'normalized_base_damage': weighted_base,
                    'final_normalized_damage': weighted_final,
                })
                group['product']['attack_power_coefficient'] += ap_coeff * count
                group['product']['spell_power_coefficient'] += sp_coeff * count
                group['product']['normalized_base_damage'] += weighted_base
                group['product']['final_normalized_damage'] += weighted_final
                group['product']['formula_components'].append({
                    'base_damage': formula_base,
                    'runtime_factors': runtime_factors,
                    'final_damage': weighted_final,
                })
        rows = []
        for group in groups.values():
            if group['component_count'] <= 0:
                continue
            product = group['product']
            base = product['normalized_base_damage']
            final = product['final_normalized_damage']
            product['runtime_multiplier'] = final / base if base else None
            rows.append(group)
        actor['actions'] = rows
        display_count += len(rows)
    result['actors'] = actors
    result['display_action_count'] = display_count
    return result


class SimcSkillDamageSnapshotService:
    """Generate one persisted exporter dataset for one SimC/DBC/schema identity."""

    EXPORTER_SCHEMA_REVISION = 7
    DATASET_SCHEMA_REVISION = 12
    TALENT_BATCH_SIZE = 12
    FIXED_PRESET = {
        'attack_power': 100.0,
        'spell_power': 100.0,
        'crit_percent': 20.0,
        'mastery_percent': 50.0,
    }

    def __init__(self, snapshot, *, backend=None):
        self.snapshot = snapshot
        self.backend = backend or SimcBackendBinary.objects.filter(identifier='production').first()

    @classmethod
    def refresh_after_dbc_update(cls):
        """Generate the latest runtime dataset once when the backend DBC build changes."""
        backend = SimcBackendBinary.objects.filter(identifier='production', is_active=True).first()
        if not backend:
            raise ValueError('未配置正式服 SimC 后端。')
        game_build = str(backend.game_build or '').strip()
        latest = SimcSkillDamageSnapshot.latest_success()
        revision = str(backend.current_version or '').strip().lower()
        latest_actors = (latest.payload or {}).get('actors') or [] if latest else []
        latest_has_complete_actor_data = bool(latest_actors) and all(
            isinstance(actor, dict)
            and actor.get('variant_model') == 'single_talent_runtime'
            and actor.get('action_universe') == 'dbc_spellbook_selected_traits_and_derived_actions'
            for actor in latest_actors
        )
        if latest and (
            latest.simc_revision == revision
            and latest.game_build == game_build
            and latest.schema_revision == cls.DATASET_SCHEMA_REVISION
            and latest_has_complete_actor_data
        ):
            return None
        service = cls.create_for_current_backend()
        service.generate()
        return service.snapshot

    @classmethod
    def create_for_current_backend(cls, requested_by_id=None):
        backend = SimcBackendBinary.objects.filter(identifier='production', is_active=True).first()
        if not backend:
            raise ValueError('未配置正式服 SimC 后端。')
        revision = str(backend.current_version or '').strip().lower()
        game_build = str(backend.game_build or '').strip()
        if len(revision) != 40 or any(ch not in '0123456789abcdef' for ch in revision):
            raise ValueError('SimC 后端缺少完整 40 位 revision。')
        if not game_build:
            raise ValueError('SimC 后端缺少 WoW/DBC game build。')
        snapshot, created = SimcSkillDamageSnapshot.objects.get_or_create(
            simc_revision=revision,
            game_build=game_build,
            schema_revision=cls.DATASET_SCHEMA_REVISION,
            defaults={'requested_by_id': requested_by_id},
        )
        if not created:
            actors = (snapshot.payload or {}).get('actors') or []
            has_complete_actor_data = bool(actors) and all(
                isinstance(actor, dict)
                and actor.get('variant_model') == 'single_talent_runtime'
                and actor.get('action_universe') == 'dbc_spellbook_selected_traits_and_derived_actions'
                for actor in actors
            )
            if snapshot.status == SimcSkillDamageSnapshot.STATUS_RUNNING or (
                snapshot.status == SimcSkillDamageSnapshot.STATUS_SUCCEEDED
                and has_complete_actor_data
            ):
                raise ValueError('该 SimC/DBC/exporter 版本已生成或正在生成。')
            snapshot.status = SimcSkillDamageSnapshot.STATUS_PENDING
            snapshot.error_text = ''
            snapshot.requested_by_id = requested_by_id
            snapshot.save(update_fields=['status', 'error_text', 'requested_by_id'])
        return cls(snapshot, backend=backend)

    def _profiles(self):
        rows = SimcProfile.objects.filter(
            is_active=True,
            user_id__isnull=True,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
        ).order_by('class_name', 'spec', '-id')
        selected = []
        seen = set()
        for profile in rows:
            key = (str(profile.class_name or '').lower(), str(profile.spec or '').lower())
            if key in seen:
                continue
            seen.add(key)
            selected.append(profile)
        if not selected:
            raise ValueError('没有可用于初始化职业模块的 active SimC 上游 Profile。')
        return selected

    def _talent_entries(self, profile):
        class_name, spec_name = canonical_simc_profile_identity(
            getattr(profile, 'spec', ''), getattr(profile, 'class_name', ''),
        )
        rows = WowTalentNodeMetadata.objects.filter(
            talent_version__is_active=True,
            class_name__iexact=class_name,
            tree_type__in=('class', 'spec', 'hero'),
            node_id__isnull=False,
        ).order_by('tree_type', 'row', 'column', 'node_id', 'id')
        allowed_hero_subtrees = set(spec_hero_subtree_names(class_name, spec_name))
        if not allowed_hero_subtrees:
            raise ValueError(f'{profile.spec} 缺少权威英雄天赋树关系。')
        selected = []
        seen = set()
        for row in rows:
            if simc_spec_slug(row.spec_name) != spec_name:
                continue
            if str(row.tree_type or '').lower() == 'hero':
                subtree_name = hero_subtree_name_by_id(row.db2_subtree_id)
                if not subtree_name or subtree_name not in allowed_hero_subtrees:
                    continue
            key = (str(row.tree_type or '').lower(), row.node_id)
            if not isinstance(row.node_id, int) or row.node_id <= 0 or key in seen:
                continue
            seen.add(key)
            selected.append(row)
        if not selected:
            raise ValueError(f'{profile.spec} 缺少当前版本单项天赋 trait entry。')
        return selected

    @staticmethod
    def _hero_talent_trees(profile, talents):
        class_name, spec_name = canonical_simc_profile_identity(
            getattr(profile, 'spec', ''), getattr(profile, 'class_name', ''),
        )
        ordered_names = spec_hero_subtree_names(class_name, spec_name)
        ids_by_name = {}
        for talent in talents:
            if str(getattr(talent, 'tree_type', '') or '').lower() != 'hero':
                continue
            subtree_id = getattr(talent, 'db2_subtree_id', None)
            subtree_name = hero_subtree_name_by_id(subtree_id)
            if subtree_name in ordered_names:
                ids_by_name.setdefault(subtree_name, subtree_id)
        missing = [name for name in ordered_names if name not in ids_by_name]
        if missing:
            raise ValueError(f'{profile.spec} 缺少英雄天赋子树元数据：{", ".join(missing)}。')
        return [
            {
                'id': ids_by_name[name],
                'name': name,
                'name_zh': hero_subtree_name_zh(name),
            }
            for name in ordered_names
        ]

    @staticmethod
    def _spec_root_scaffold(talents):
        roots = [
            talent for talent in talents
            if str(getattr(talent, 'tree_type', '') or '').lower() == 'spec'
            and not (getattr(talent, 'parents_json', None) or [])
            and isinstance(getattr(talent, 'row', None), int)
        ]
        if not roots:
            return []
        first_row = min(talent.row for talent in roots)
        candidates = [talent for talent in roots if talent.row == first_row]
        # First-row roots can be choice/mutually-exclusive entries. Selecting
        # every root makes such actors invalid; one deterministic granted entry
        # is enough to initialize the spec action scaffold.
        return [min(candidates, key=lambda talent: (
            0 if int(getattr(talent, 'flags', 0) or 0) & 8 else 1,
            int(getattr(talent, 'column', 0) or 0),
            int(getattr(talent, 'node_id', 0) or 0),
            int(getattr(talent, 'pk', 0) or 0),
        ))]

    @staticmethod
    def _implicit_prerequisite_nodes(profile):
        """Return non-selectable hero graph anchors needed to close metadata paths."""
        class_name, spec_name = canonical_simc_profile_identity(
            getattr(profile, 'spec', ''), getattr(profile, 'class_name', ''),
        )
        rows = WowTalentNodeMetadata.objects.filter(
            talent_version__is_active=True,
            class_name__iexact=class_name,
            tree_type='hero_anchor',
            node_id__isnull=False,
        ).order_by('node_id', 'id')
        selected = []
        seen = set()
        for row in rows:
            if simc_spec_slug(row.spec_name) != spec_name or row.node_id in seen:
                continue
            seen.add(row.node_id)
            selected.append(row)
        return selected

    @staticmethod
    def _talent_prerequisite_map(talents, *, metadata_nodes=(), entry_order=None):
        """Resolve each trait's transitive selectable prerequisite closure."""
        by_entry = {}
        for talent in [*talents, *metadata_nodes]:
            entry_id = getattr(talent, 'node_id', None)
            if not isinstance(entry_id, int) or entry_id <= 0:
                raise ValueError('单项天赋缺少有效 SimC trait entry。')
            if entry_id in by_entry:
                raise ValueError(f'天赋前置元数据包含重复 trait entry：{entry_id}')
            by_entry[entry_id] = talent

        resolved = {}
        visiting = set()
        selectable_tree_types = {'class', 'spec', 'hero'}

        def is_implicit(talent):
            tree_type = str(getattr(talent, 'tree_type', '') or '').lower()
            return tree_type not in selectable_tree_types

        def path_identity(path):
            return tuple(
                (str(getattr(item, 'tree_type', '') or '').lower(), item.node_id)
                for item in path
            )

        def visit(talent):
            if talent.pk in resolved:
                return resolved[talent.pk]
            if talent.pk in visiting:
                raise ValueError(f'天赋前置元数据存在循环：{talent.node_id}')
            visiting.add(talent.pk)
            candidate_paths = []
            path_errors = []
            try:
                for parent_id in (getattr(talent, 'parents_json', None) or []):
                    try:
                        if (
                            not isinstance(parent_id, int)
                            or parent_id <= 0
                            or parent_id not in by_entry
                        ):
                            raise ValueError(
                                f'天赋 {talent.node_id} 缺少有效前置 trait entry：{parent_id}'
                            )
                        parent = by_entry[parent_id]
                        if is_implicit(parent):
                            candidate_paths.append([])
                            continue
                        path = [*visit(parent), parent]
                        deduplicated = []
                        seen = set()
                        for prerequisite in path:
                            identity = (
                                str(getattr(prerequisite, 'tree_type', '') or '').lower(),
                                prerequisite.node_id,
                            )
                            if identity not in seen:
                                seen.add(identity)
                                deduplicated.append(prerequisite)
                        candidate_paths.append(deduplicated)
                    except ValueError as exc:
                        path_errors.append(exc)
            finally:
                visiting.remove(talent.pk)
            if not candidate_paths and path_errors:
                raise ValueError(
                    f'天赋 {talent.node_id} 没有有效前置路径：{path_errors[0]}'
                ) from path_errors[0]
            result = min(
                candidate_paths,
                key=lambda path: (len(path), path_identity(path)),
                default=[],
            )
            resolved[talent.pk] = result
            return result

        result = {talent.pk: visit(talent) for talent in talents}
        entry_order = entry_order or {}
        by_talent_id = {}
        for talent in talents:
            talent_id = getattr(talent, 'talent_id', None)
            if talent_id:
                by_talent_id.setdefault(talent_id, []).append(talent)
        for group in by_talent_id.values():
            apex_nodes = [
                {
                    'talent_id': getattr(talent, 'talent_id', None),
                    'max_points': getattr(talent, 'max_points', None),
                }
                for talent in group
            ]
            if (
                not TalentMetadataProvider.is_apex_entry_group(
                    apex_nodes[0] if apex_nodes else {}, apex_nodes,
                )
                or any(
                    getattr(row, 'node_id', None) not in entry_order
                    for row in group
                )
            ):
                continue
            ordered = sorted(
                group,
                key=lambda row: entry_order[getattr(row, 'node_id')],
            )
            preceding = []
            for talent in ordered:
                merged = []
                for prerequisite in [*result[talent.pk], *preceding]:
                    if prerequisite.pk != talent.pk and prerequisite not in merged:
                        merged.append(prerequisite)
                result[talent.pk] = merged
                preceding.append(talent)
        return result

    def _binary_path(self):
        config = getattr(settings, 'SIMC_CONFIG', {}) or {}
        configured = str(config.get('simc_path') or '')
        path = str(configured or getattr(self.backend, 'simc_path', '') or '')
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise ValueError('SimC exporter 二进制不可执行。')
        return path

    def _run_profile_export(
        self, profile, talents, *, scaffold_talents=(), talent_prerequisites=None,
        target_health=100,
    ):
        baseline_profile = copy.copy(profile)
        baseline_profile.talent = ''
        reference_input = SimcComposer(None).compose_validation_input(baseline_profile, '')
        class_name, specialization = canonical_simc_profile_identity(
            getattr(profile, 'spec', ''), getattr(profile, 'class_name', ''),
        )
        if class_name == 'warlock' and specialization == 'destruction':
            reference_input = (
                reference_input.rstrip()
                + '\nwarlock.normalize_destruction_mastery=1\n'
            )
        simc_input = build_single_talent_actor_input(
            reference_input, profile.class_name, talents,
            scaffold_talents=scaffold_talents,
            talent_prerequisites=talent_prerequisites,
        )
        with tempfile.TemporaryDirectory(prefix='simc-skill-damage-') as tmp:
            input_path = Path(tmp) / 'actors.simc'
            output_path = Path(tmp) / 'export.json'
            input_path.write_text(simc_input, encoding='utf-8')
            command = [
                self._binary_path(), str(input_path),
                f'skill_damage_target_health_percentage={target_health}',
                f'skill_damage_export={output_path}',
                f'skill_damage_revision={self.snapshot.simc_revision}',
                f'skill_damage_game_build={self.snapshot.game_build}',
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            if result.returncode != 0 or not output_path.exists():
                diagnostic = (result.stderr or result.stdout or 'SimC exporter 未生成 JSON').strip()
                raise RuntimeError(diagnostic[-2000:])
            payload = json.loads(output_path.read_text(encoding='utf-8'))
        expected_actor_names = {
            'skill_damage_base',
            *(f'skill_damage_reference_{talent.pk}_trait_{talent.node_id}' for talent in talents),
            *(f'skill_damage_talent_{talent.pk}_trait_{talent.node_id}' for talent in talents),
        }
        self._validate_export(
            payload, profile=profile, expected_actor_names=expected_actor_names,
        )
        return payload

    def _validate_export(
        self, payload, *, profile=None, expected_actor_count=1,
        expected_actor_names=None,
    ):
        if payload.get('schema_version') != self.EXPORTER_SCHEMA_REVISION:
            raise ValueError('exporter schema revision 不匹配。')
        if payload.get('simc_revision') != self.snapshot.simc_revision:
            raise ValueError('exporter SimC revision 不匹配。')
        if payload.get('game_build') != self.snapshot.game_build:
            raise ValueError('exporter game build 不匹配。')
        normalization = payload.get('normalization_basis') or {}
        if normalization != self.FIXED_PRESET:
            raise ValueError('exporter 未按 AP/SP=100、暴击=20%、精通=50% 的固定预制生成。')
        actors = payload.get('actors')
        if not isinstance(actors, list):
            raise ValueError('exporter actors 结构无效。')
        if expected_actor_names is not None:
            expected_actor_names = set(expected_actor_names)
            actor_names = [
                actor.get('name') if isinstance(actor, dict) else None
                for actor in actors
            ]
            if len(actor_names) != len(set(actor_names)) or set(actor_names) != expected_actor_names:
                raise ValueError(
                    '单项天赋 exporter actor 名称集合无效：必须且只能包含预期 actor，且名称唯一。'
                )
        elif len(actors) != expected_actor_count:
            raise ValueError(f'单项天赋 exporter actor 数量无效：期望 {expected_actor_count}，实际 {len(actors)}。')
        required_amount_fields = (
            'hit', 'crit', 'crit_multiplier', 'crit_chance',
            'crit_chance_uncapped', 'expected',
        )
        required_dbc_fields = (
            'attack_power_coefficient', 'spell_power_coefficient',
            'normalized_base', 'effect_indexes',
        )

        def validate_amount(amount, *, context):
            if not isinstance(amount, dict):
                raise ValueError(f'exporter action 缺少 {context} 数学期望。')
            present = [
                (name, amount.get(name))
                for name in ('direct', 'tick')
                if amount.get(name) is not None
            ]
            unresolved_reason = amount.get('unresolved_reason')
            if not present:
                if unresolved_reason:
                    return
                raise ValueError(f'exporter action {context} 没有可展示的伤害组件。')
            for component_name, component in present:
                if not isinstance(component, dict) or any(
                    field not in component for field in required_amount_fields
                ):
                    raise ValueError('exporter 数学期望字段无效。')
                if not isinstance(component.get('can_crit'), bool):
                    raise ValueError('exporter can_crit 必须为布尔值。')
                base_damage_layers = component.get('base_damage_layers')
                if (
                    not isinstance(base_damage_layers, dict)
                    or set(base_damage_layers) != {'base_multiplier', 'component_multiplier'}
                    or not all(
                        _finite_number(value) and value > 0
                        for value in base_damage_layers.values()
                    )
                ):
                    raise ValueError('exporter base damage layers 结构或数值无效。')
                expected_runtime_fields = set(_RUNTIME_LAYER_FIELDS[component_name])
                runtime_layers = component.get('runtime_layers')
                if (
                    not isinstance(runtime_layers, dict)
                    or set(runtime_layers) != expected_runtime_fields
                    or not all(
                        _finite_number(value) and value > 0
                        for value in runtime_layers.values()
                    )
                ):
                    raise ValueError('exporter runtime layers 结构或数值无效。')
                equivalent_count = component.get('damage_equivalent_count')
                if not _finite_number(equivalent_count) or equivalent_count <= 0:
                    raise ValueError('exporter damage equivalent count 无效。')
                values = [component[field] for field in required_amount_fields]
                valid = (
                    all(value is None or _finite_number(value) for value in values)
                    if unresolved_reason else all(_finite_number(value) for value in values)
                )
                if not valid:
                    raise ValueError('exporter 数学期望字段无效。')
                if unresolved_reason and not all(_finite_number(value) for value in values):
                    continue
                crit_chance = component['crit_chance']
                crit_chance_uncapped = component['crit_chance_uncapped']
                expected_crit_chance = min(1.0, max(0.0, crit_chance_uncapped))
                if (
                    not math.isclose(
                        crit_chance, expected_crit_chance,
                        rel_tol=1.0e-8, abs_tol=1.0e-8,
                    )
                    or (
                        component['can_crit'] is False
                        and (
                            not math.isclose(crit_chance, 0.0, abs_tol=1.0e-8)
                            or not math.isclose(crit_chance_uncapped, 0.0, abs_tol=1.0e-8)
                        )
                    )
                ):
                    raise ValueError('exporter 暴击率一致性无效。')
                hit_decimal = Decimal(str(component['hit']))
                crit_decimal = Decimal(str(component['crit']))
                crit_chance_decimal = Decimal(str(crit_chance))
                calculated_decimal = (
                    hit_decimal * (Decimal('1') - crit_chance_decimal)
                    + crit_decimal * crit_chance_decimal
                )
                expected_decimal = Decimal(str(component['expected']))
                rounded_error = abs(expected_decimal - calculated_decimal).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP,
                )
                if rounded_error > Decimal('0.01'):
                    raise ValueError(
                        'exporter 数学期望一致性无效：'
                        f'{context}.{component_name} expected={component["expected"]!r}, '
                        f'calculated={calculated_decimal!r}, '
                        f'damage_equivalent_count={component.get("damage_equivalent_count")!r}。'
                    )

        def validate_scenarios(scenarios, *, actor_buff_identities):
            if not isinstance(scenarios, list):
                raise ValueError('exporter action scenarios 结构无效。')
            scenario_identities = set()
            for scenario in scenarios:
                if not isinstance(scenario, dict) or not isinstance(scenario.get('buffs'), list):
                    raise ValueError('exporter scenario 结构无效。')
                buff_tokens = []
                scenario_buff_identities = {}
                for buff in scenario['buffs']:
                    buff_token = buff.get('token') if isinstance(buff, dict) else None
                    buff_scope = buff.get('scope') if isinstance(buff, dict) else None
                    if not isinstance(buff_token, str) or not buff_token.strip():
                        raise ValueError('exporter scenario buff token identity 无效。')
                    if buff_scope not in {'self', 'target'}:
                        raise ValueError('exporter scenario buff scope 无效。')
                    if (
                        not isinstance(buff.get('spell_id'), int)
                        or isinstance(buff.get('spell_id'), bool)
                        or buff.get('spell_id') < 0
                    ):
                        raise ValueError('exporter scenario buff spell identity 无效。')
                    buff_token = buff_token.strip()
                    scenario_buff_identities[buff_token] = (buff.get('spell_id'), buff_scope)
                    buff_tokens.append(buff_token)
                if not buff_tokens or len(buff_tokens) != len(set(buff_tokens)):
                    raise ValueError('exporter scenario buff token identity 必须非空且唯一。')
                for buff_token, buff_identity in scenario_buff_identities.items():
                    previous_identity = actor_buff_identities.get(buff_token)
                    if previous_identity is not None and previous_identity != buff_identity:
                        raise ValueError('exporter actor scenario buff canonical identity 冲突。')
                    actor_buff_identities[buff_token] = buff_identity
                scenario_identity = tuple(sorted(buff_tokens))
                if scenario_identity in scenario_identities:
                    raise ValueError('exporter scenario token identity 重复。')
                scenario_identities.add(scenario_identity)
                validate_amount(scenario.get('values'), context='scenario')

        for actor in actors:
            if not isinstance(actor, dict) or not isinstance(actor.get('actions'), list):
                raise ValueError('exporter actor/actions 结构无效。')
            if actor.get('talent_effectiveness') not in {'active', 'inactive', 'unknown'}:
                raise ValueError('exporter actor talent effectiveness 无效。')
            actor_class = actor.get('class')
            actor_spec = actor.get('spec')
            if (
                not isinstance(actor_class, str) or not actor_class.strip()
                or not isinstance(actor_spec, str) or not actor_spec.strip()
                or actor.get('action_universe')
                != 'dbc_spellbook_selected_traits_and_derived_actions'
            ):
                raise ValueError('exporter actor 身份或 action universe 无效。')
            if profile is not None:
                expected_class = str(getattr(profile, 'class_name', '') or '').strip().lower()
                expected_spec = str(getattr(profile, 'spec', '') or '').strip().lower()
                prefix = f'{expected_class}_'
                if expected_class and expected_spec.startswith(prefix):
                    expected_spec = expected_spec[len(prefix):]
                if (
                    expected_class and actor_class.strip().lower() != expected_class
                    or expected_spec and actor_spec.strip().lower() != expected_spec
                ):
                    raise ValueError('exporter actor 身份与请求 Profile 不匹配。')
            action_identities = set()
            actor_buff_identities = {}
            for action in actor['actions']:
                if not isinstance(action, dict):
                    raise ValueError('exporter action 结构无效。')
                token = action.get('token')
                spell_id = action.get('spell_id')
                if (
                    not isinstance(token, str) or not token.strip()
                    or not isinstance(spell_id, int) or isinstance(spell_id, bool) or spell_id < 0
                ):
                    raise ValueError('exporter action token identity 无效。')
                action_identity = (token.strip(), spell_id)
                if action_identity in action_identities:
                    raise ValueError('exporter action token identity 重复。')
                action_identities.add(action_identity)
                if not isinstance(action.get('supported'), bool):
                    raise ValueError('exporter action supported 必须为布尔值。')
                if not isinstance(action.get('player_skill'), bool):
                    raise ValueError('exporter action player skill 必须为布尔值。')
                if (
                    not isinstance(action.get('reporting_root_token'), str)
                    or not action['reporting_root_token'].strip()
                    or not isinstance(action.get('reporting_root_spell_id'), int)
                    or isinstance(action.get('reporting_root_spell_id'), bool)
                    or action['reporting_root_spell_id'] < 0
                    or not isinstance(action.get('reporting_root_component'), bool)
                ):
                    raise ValueError('exporter action reporting root 结构无效。')
                validate_scenarios(
                    action.get('scenarios'), actor_buff_identities=actor_buff_identities,
                )
                if action['supported'] is False:
                    if not action.get('unsupported_reason'):
                        raise ValueError('exporter unsupported action 缺少原因。')
                    continue
                dbc_scaling = action.get('dbc_scaling')
                if (
                    not isinstance(dbc_scaling, dict)
                    or dbc_scaling.get('source') != 'spell_effect'
                    or not isinstance(dbc_scaling.get('requires_weapon_data'), bool)
                ):
                    raise ValueError('exporter action 缺少有效的 DBC SpellEffect scaling。')
                for component_name in ('direct', 'tick'):
                    component = dbc_scaling.get(component_name)
                    if component is None:
                        continue
                    if not isinstance(component, dict) or any(
                        field not in component for field in required_dbc_fields
                    ):
                        raise ValueError('exporter DBC SpellEffect 组件结构无效。')
                    coefficients = [
                        component['attack_power_coefficient'],
                        component['spell_power_coefficient'],
                        component['normalized_base'],
                    ]
                    indexes = component['effect_indexes']
                    if (
                        not all(
                            isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and math.isfinite(value)
                            for value in coefficients
                        )
                        or not isinstance(indexes, list)
                        or not indexes
                        or not all(
                            isinstance(index, int) and not isinstance(index, bool) and index >= 0
                            for index in indexes
                        )
                    ):
                        raise ValueError('exporter DBC SpellEffect 组件数值无效。')
                    expected_base = 100.0 * (coefficients[0] + coefficients[1])
                    if not math.isclose(coefficients[2], expected_base, rel_tol=1e-9, abs_tol=1e-6):
                        raise ValueError('exporter DBC SpellEffect 归一化基础伤害无效。')
                validate_amount(action.get('baseline'), context='baseline')

    def _run_profile_target_resilient(
        self, profile, talents, *, scaffold_talents, talent_prerequisites, target_health,
    ):
        """Export all actors while isolating a SimC process crash to the smallest talent input."""
        baseline_export = self._run_profile_export(
            profile, [], scaffold_talents=scaffold_talents, target_health=target_health,
        )
        baseline_map = {
            str(actor.get('name') or ''): actor
            for actor in (baseline_export.get('actors') or [])
            if isinstance(actor, dict)
        }
        baseline = baseline_map.get('skill_damage_base')
        if baseline is None or set(baseline_map) != {'skill_damage_base'}:
            raise ValueError(f'{profile.spec} exporter 缺少独立基线 actor。')

        exported_actors = {}
        unresolved = list(baseline_export.get('unresolved') or [])
        _class_name, specialization = canonical_simc_profile_identity(
            getattr(profile, 'spec', ''), getattr(profile, 'class_name', ''),
        )

        def export_batch(batch):
            try:
                payload = self._run_profile_export(
                    profile, batch,
                    scaffold_talents=scaffold_talents,
                    talent_prerequisites=talent_prerequisites,
                    target_health=target_health,
                )
            except RuntimeError as exc:
                diagnostic = str(exc)
                fatal_actor_initialization = re.search(
                    r'(?:^|\r?\n)sim_signal_handler: Segmentation fault!'
                    r'(?:[ \t]+(?:signal_\d+\b|Iteration=-?\d+\b)[^\r\n]*)?'
                    r'(?:\r?\n|$)',
                    diagnostic,
                ) or re.search(
                    r'(?:^|\r?\n)simc: class_modules/[^\r\n]+:'
                    r'[^\r\n]*\bAssertion [^\r\n]+ failed\.(?:\r?\n|$)',
                    diagnostic,
                ) or re.search(
                    r"(?:^|\r?\n)Error: Player '[^'\r\n]+' could not find spell data "
                    r"for Action '[^'\r\n]+' \(\d+\)\.(?:\r?\n|$)",
                    diagnostic,
                )
                if not fatal_actor_initialization:
                    raise
                if len(batch) > 1:
                    middle = len(batch) // 2
                    export_batch(batch[:middle])
                    export_batch(batch[middle:])
                    return
                talent = batch[0]
                unresolved.append({
                    'class': str(getattr(profile, 'class_name', '') or ''),
                    'specialization': specialization,
                    'target_health_percentage': target_health,
                    'talent': {
                        'id': talent.node_id,
                        'metadata_id': talent.pk,
                        'name': str(talent.name or ''),
                        'name_zh': str(talent.name_zh or ''),
                        'tree_type': str(talent.tree_type or ''),
                    },
                    'reason': 'simc_actor_initialization_failed',
                    'diagnostic': str(exc)[-2000:],
                })
                return

            actor_map = {
                str(actor.get('name') or ''): actor
                for actor in (payload.get('actors') or [])
                if isinstance(actor, dict)
            }
            current_baseline = actor_map.pop('skill_damage_base', None)
            if current_baseline != baseline:
                raise ValueError(f'{profile.spec} 分块 exporter 基线 actor 不一致。')
            duplicate_names = set(exported_actors).intersection(actor_map)
            if duplicate_names:
                raise ValueError(f'{profile.spec} 分块 exporter 包含重复天赋 actor。')
            exported_actors.update(actor_map)
            unresolved.extend(payload.get('unresolved') or [])

        for start in range(0, len(talents), self.TALENT_BATCH_SIZE):
            export_batch(talents[start:start + self.TALENT_BATCH_SIZE])
        return baseline, exported_actors, unresolved

    def generate(self):
        now = timezone.now()
        SimcSkillDamageSnapshot.objects.filter(pk=self.snapshot.pk).update(
            status=SimcSkillDamageSnapshot.STATUS_RUNNING,
            started_at=now,
            completed_at=None,
            error_text='',
        )
        self.snapshot.refresh_from_db()
        try:
            actors = []
            unresolved = []
            unresolved_keys = set()
            for profile in self._profiles():
                all_talents = self._talent_entries(profile)
                hero_talent_trees = self._hero_talent_trees(profile, all_talents)
                scaffold_talents = self._spec_root_scaffold(all_talents)
                talent_version_ids = {
                    getattr(talent, 'talent_version_id', None)
                    for talent in all_talents
                }
                if talent_version_ids == {None}:
                    entry_order = {}
                else:
                    if None in talent_version_ids or len(talent_version_ids) != 1:
                        raise ValueError(f'{profile.spec} 单项天赋混入多个 active 版本。')
                    talent_version = getattr(all_talents[0], 'talent_version', None)
                    if talent_version is None:
                        raise ValueError(f'{profile.spec} 单项天赋缺少 active 版本对象。')
                    entry_order = TalentMetadataProvider(
                        talent_version=talent_version,
                    ).get_choice_entry_order()
                talent_prerequisites = self._talent_prerequisite_map(
                    all_talents,
                    metadata_nodes=self._implicit_prerequisite_nodes(profile),
                    entry_order=entry_order,
                )
                scaffold_identities = {
                    (
                        str(getattr(talent, 'tree_type', '') or '').strip().lower(),
                        getattr(talent, 'node_id', None),
                    )
                    for talent in scaffold_talents
                }
                talents = [
                    talent for talent in all_talents
                    if (
                        str(getattr(talent, 'tree_type', '') or '').strip().lower(),
                        getattr(talent, 'node_id', None),
                    ) not in scaffold_identities
                ]
                base_high, high_actors, high_unresolved = self._run_profile_target_resilient(
                    profile, talents, scaffold_talents=scaffold_talents,
                    talent_prerequisites=talent_prerequisites, target_health=100,
                )
                base_low, low_actors, low_unresolved = self._run_profile_target_resilient(
                    profile, talents, scaffold_talents=scaffold_talents,
                    talent_prerequisites=talent_prerequisites, target_health=34,
                )
                for row in [*high_unresolved, *low_unresolved]:
                    key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                    if key not in unresolved_keys:
                        unresolved_keys.add(key)
                        unresolved.append(row)

                variants = []
                for talent in talents:
                    identity = f'{talent.pk}_trait_{talent.node_id}'
                    actor_name = f'skill_damage_talent_{identity}'
                    reference_name = f'skill_damage_reference_{identity}'
                    high_actor = high_actors.get(actor_name)
                    low_actor = low_actors.get(actor_name)
                    reference_high = high_actors.get(reference_name)
                    reference_low = low_actors.get(reference_name)
                    if not high_actor and not low_actor:
                        # Both target-specific failures are already preserved in unresolved.
                        continue
                    if (high_actor and not reference_high) or (low_actor and not reference_low):
                        raise ValueError(f'{profile.spec} 天赋 {talent.node_id} 缺少对应前置 runtime actor。')
                    hero_subtree_id = (
                        talent.db2_subtree_id if str(talent.tree_type or '').lower() == 'hero' else None
                    )
                    hero_subtree_name = hero_subtree_name_by_id(hero_subtree_id)
                    variants.append({
                        'talent': {
                            'id': talent.pk,
                            'node_id': talent.node_id,
                            'tree_type': talent.tree_type,
                            'hero_subtree_id': hero_subtree_id,
                            'hero_subtree_name': hero_subtree_name,
                            'hero_subtree_name_zh': hero_subtree_name_zh(hero_subtree_name),
                            'name': talent.name,
                            'name_zh': talent.name_zh,
                            'description': talent.description,
                            'description_zh': talent.description_zh,
                        },
                        'reference_high': reference_high,
                        'reference_low': reference_low,
                        'high': high_actor,
                        'low': low_actor,
                    })

                actor = copy.deepcopy(base_high)
                actor.pop('name', None)
                if 'specialization' not in actor and actor.get('spec'):
                    actor['specialization'] = actor.pop('spec')
                actor['variant_model'] = 'single_talent_runtime'
                actor['hero_talent_trees'] = hero_talent_trees
                actor['base_damage_basis'] = 'dbc_spell_effect_ap_sp_coefficients_at_100'
                global_effects = classify_global_skill_effects(base_high, base_low, variants)
                actor['global_skill_effects'] = global_effects
                actor['actions'] = flatten_single_talent_damage_variants(
                    base_high, base_low, variants, global_effects=global_effects,
                )
                actors.append(actor)
            payload = localize_skill_damage_payload({
                'identity': {
                    'simc_revision': self.snapshot.simc_revision,
                    'game_build': self.snapshot.game_build,
                    'schema_revision': self.snapshot.schema_revision,
                },
                'preset': dict(self.FIXED_PRESET),
                'actors': actors,
                'unresolved': unresolved,
            })
            action_count = sum(len(actor.get('actions') or []) for actor in actors)
            self.snapshot.status = SimcSkillDamageSnapshot.STATUS_SUCCEEDED
            self.snapshot.payload = payload
            self.snapshot.generated_spec_count = len(actors)
            self.snapshot.generated_action_count = action_count
            self.snapshot.completed_at = timezone.now()
            self.snapshot.error_text = ''
            self.snapshot.save(update_fields=[
                'status', 'payload', 'generated_spec_count', 'generated_action_count',
                'completed_at', 'error_text',
            ])
            return payload
        except Exception as exc:
            self.snapshot.status = SimcSkillDamageSnapshot.STATUS_FAILED
            self.snapshot.error_text = str(exc)[:4000]
            self.snapshot.completed_at = timezone.now()
            self.snapshot.save(update_fields=['status', 'error_text', 'completed_at'])
            raise
        finally:
            close_old_connections()
