import copy
import json
import math
import os
import re
import subprocess
import tempfile
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
from botend.services.simc_player_config import canonical_simc_profile_identity, simc_spec_slug


def _text_key(value):
    return str(value or '').strip().casefold()


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
        action.get('spell_id')
        for actor in actors for action in (actor.get('actions') or [])
        if isinstance(action, dict) and isinstance(action.get('spell_id'), int)
    }
    tokens = {
        _text_key(action.get('token'))
        for actor in actors for action in (actor.get('actions') or [])
        if isinstance(action, dict) and _text_key(action.get('token'))
    }
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

        for action in actor.get('actions') or []:
            if not isinstance(action, dict):
                continue
            token = _text_key(action.get('token'))
            spell_id = action.get('spell_id')
            apl_token_name = _single_top_name(
                [row for row in apl_rows if _text_key(row.get('symbol__token')) == token],
                scope_rank,
            ) if token else ''
            talent_name = _single_top_name(
                [row for row in talent_rows if spell_id in (
                    row.get('spell_id'), row.get('display_spell_id'),
                )],
                lambda row: scope_rank(row, talent=True),
            ) if isinstance(spell_id, int) else ''
            apl_spell_name = _single_top_name(
                [row for row in apl_rows if row.get('spell_id') == spell_id],
                scope_rank,
            ) if isinstance(spell_id, int) else ''
            action['display_name'] = (
                str(action.get('display_name') or '').strip()
                or spell_names.get(spell_id) or apl_token_name or talent_name or apl_spell_name
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
        output.extend(actor_block(f'skill_damage_reference_{trait.pk}', prerequisites))
        output.extend(actor_block(f'skill_damage_talent_{trait.pk}', [*prerequisites, trait]))
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


_AMOUNT_COMPONENT_FIELDS = ('hit', 'crit', 'crit_multiplier', 'crit_chance', 'expected')


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
            tuple((field, component.get(field)) for field in _AMOUNT_COMPONENT_FIELDS),
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
        for field in _AMOUNT_COMPONENT_FIELDS:
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


def flatten_single_talent_damage_variants(base_high, base_low, variants):
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

    def append_row(action, amount, *, talent, condition, comparison, scenario_tokens=()):
        if not isinstance(action, dict) or _amount_state(amount)[0] != 'resolved':
            return
        row = copy.deepcopy(action)
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
                condition='无单项增伤天赋', comparison=high_amount,
            )
        if _amount_state(low_amount)[0] == 'resolved' and (
            _amount_state(high_amount)[0] != 'resolved' or _effect_changed(high_amount, low_amount)
        ):
            append_row(
                low_action, low_amount, talent=no_talent,
                condition='目标生命值低于 35%（无单项增伤天赋）',
                comparison=low_amount,
            )

    for item in variants:
        talent = item.get('talent') or {}
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
            base_high_action = reference_high_actions.get(identity)
            base_low_action = reference_low_actions.get(identity)
            high_amount = high_action.get('baseline') if high_action else None
            low_amount = low_action.get('baseline') if low_action else None
            base_high_amount = base_high_action.get('baseline') if base_high_action else None
            base_low_amount = base_low_action.get('baseline') if base_low_action else None
            candidates = []

            if _amount_state(high_amount)[0] == 'resolved' and _effect_changed(base_high_amount, high_amount):
                candidates.append((
                    high_action, high_amount, base_high_amount,
                    '单项天赋常驻', (),
                ))

            base_high_scenarios = _scenario_amounts(base_high_action)
            high_scenarios = _scenario_amounts(high_action)
            base_low_scenarios = _scenario_amounts(base_low_action)
            low_scenarios = _scenario_amounts(low_action)

            for tokens, amount in high_scenarios.items():
                reference = base_high_scenarios.get(tokens, base_high_amount)
                if _amount_state(amount)[0] == 'resolved' and _effect_changed(reference, amount):
                    candidates.append((
                        high_action, amount, reference,
                        f'探针条件：启用 {" + ".join(tokens)} buff', tokens,
                    ))

            if _amount_state(low_amount)[0] == 'resolved' and _paired_effect_changed(
                base_high_amount, high_amount, base_low_amount, low_amount,
            ):
                candidates.append((
                    low_action, low_amount, base_low_amount,
                    '目标生命值低于 35%', (),
                ))

            for tokens, amount in low_scenarios.items():
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
    """Return the display read model while preserving raw snapshot diagnostics."""
    result = copy.deepcopy(payload or {})
    display_count = 0
    required = (
        'current_talent_damage', 'crit_damage', 'crit_multiplier',
        'actual_crit_chance', 'normalized_expected',
    )
    actors = [actor for actor in (result.get('actors') or []) if isinstance(actor, dict)]
    for actor in actors:
        rows = []
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
                row = {
                    key: copy.deepcopy(value)
                    for key, value in action.items()
                    if key not in ('baseline', 'scenarios', 'unsupported_reason')
                }
                row['component'] = component_name
                row['product'] = copy.deepcopy(product)
                rows.append(row)
        actor['actions'] = rows
        display_count += len(rows)
    result['actors'] = actors
    result['display_action_count'] = display_count
    return result


class SimcSkillDamageSnapshotService:
    """Generate one persisted exporter dataset for one SimC/DBC/schema identity."""

    EXPORTER_SCHEMA_REVISION = 3
    DATASET_SCHEMA_REVISION = 6
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
    def _talent_prerequisite_map(talents, *, metadata_nodes=()):
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

        return {talent.pk: visit(talent) for talent in talents}

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
            *(f'skill_damage_reference_{talent.pk}' for talent in talents),
            *(f'skill_damage_talent_{talent.pk}' for talent in talents),
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
        required_amount_fields = ('hit', 'crit', 'crit_multiplier', 'crit_chance', 'expected')
        required_dbc_fields = (
            'attack_power_coefficient', 'spell_power_coefficient',
            'normalized_base', 'effect_indexes',
        )
        for actor in actors:
            if not isinstance(actor, dict) or not isinstance(actor.get('actions'), list):
                raise ValueError('exporter actor/actions 结构无效。')
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
            for action in actor['actions']:
                if not isinstance(action, dict):
                    raise ValueError('exporter action 结构无效。')
                if not isinstance(action.get('supported'), bool):
                    raise ValueError('exporter action supported 必须为布尔值。')
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
                baseline = action.get('baseline')
                if not isinstance(baseline, dict):
                    raise ValueError('exporter action 缺少 baseline 数学期望。')
                components = [baseline.get('direct'), baseline.get('tick')]
                present = [component for component in components if component is not None]
                unresolved_reason = baseline.get('unresolved_reason')
                if not present:
                    if unresolved_reason:
                        continue
                    raise ValueError('exporter action 没有可展示的伤害组件。')
                for component in present:
                    if not isinstance(component, dict) or any(
                        field not in component for field in required_amount_fields
                    ):
                        raise ValueError('exporter 数学期望字段无效。')
                    values = [component[field] for field in required_amount_fields]
                    if unresolved_reason:
                        valid = all(
                            value is None or (
                                isinstance(value, (int, float))
                                and not isinstance(value, bool)
                                and math.isfinite(value)
                            )
                            for value in values
                        )
                    else:
                        valid = all(
                            isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and math.isfinite(value)
                            for value in values
                        )
                    if not valid:
                        raise ValueError('exporter 数学期望字段无效。')

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
                if not re.match(r'^sim_signal_handler: Segmentation fault!(?:\s|$)', diagnostic):
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
                talent_prerequisites = self._talent_prerequisite_map(
                    all_talents,
                    metadata_nodes=self._implicit_prerequisite_nodes(profile),
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
                    actor_name = f'skill_damage_talent_{talent.pk}'
                    reference_name = f'skill_damage_reference_{talent.pk}'
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
                actor['actions'] = flatten_single_talent_damage_variants(base_high, base_low, variants)
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
