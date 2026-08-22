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

from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, SimcBackendBinary, SimcProfile,
    SimcSkillDamageSnapshot, WowTalentNodeMetadata,
)
from botend.services.simc_composer import SimcComposer


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
                apl_token_name or talent_name or apl_spell_name
                or str(action.get('name') or action.get('token') or '未命名技能')
            )
    return result


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def build_single_talent_actor_input(profile_input, class_name, talents, *, scaffold_talents=()):
    """Expand one reference actor into a generated scaffold plus one actor per trait entry."""
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

    def actor_block(name, trait=None):
        block = list(actor_lines)
        block[0] = f'{class_name}="{name}"'
        selected = [*scaffold_talents]
        if trait is not None:
            selected.append(trait)
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

    output = [*global_lines, *actor_block('skill_damage_base')]
    for trait in talents:
        output.extend(actor_block(f'skill_damage_talent_{trait.pk}', trait))
    return '\n'.join(output).rstrip() + '\n'


def _action_identity(action):
    return (str(action.get('token') or ''), action.get('spell_id'))


def _scenario_tokens(scenario):
    if isinstance(scenario.get('active_buffs'), list):
        return tuple(str(token) for token in scenario['active_buffs'] if token)
    return tuple(
        str(buff.get('token') or '')
        for buff in (scenario.get('buffs') or [])
        if isinstance(buff, dict) and buff.get('token')
    )


def _amount_expected(amount):
    if not isinstance(amount, dict) or amount.get('unresolved_reason'):
        return None
    values = []
    for component_name in ('direct', 'tick'):
        component = amount.get(component_name)
        if isinstance(component, dict) and _finite_number(component.get('expected')):
            values.append(float(component['expected']))
    return sum(values) if values else None


def _amount_changed(left, right):
    left_value = _amount_expected(left)
    right_value = _amount_expected(right)
    if left_value is None or right_value is None:
        return False
    return not math.isclose(left_value, right_value, rel_tol=1e-8, abs_tol=1e-8)


def _effect_signature(reference, current):
    """Describe an exported effect without treating an absent action as zero."""
    reference_value = _amount_expected(reference)
    current_value = _amount_expected(current)
    if reference_value is None and current_value is None:
        return ('absent', 0.0)
    if reference_value is None:
        return ('introduced', current_value)
    if current_value is None:
        return ('removed', reference_value)
    return ('delta', current_value - reference_value)


def _effect_changed(reference, current):
    kind, value = _effect_signature(reference, current)
    return kind != 'delta' or not math.isclose(value, 0.0, rel_tol=1e-8, abs_tol=1e-8)


def _paired_effect_changed(high_reference, high_current, low_reference, low_current):
    high_kind, high_value = _effect_signature(high_reference, high_current)
    low_kind, low_value = _effect_signature(low_reference, low_current)
    return high_kind != low_kind or not math.isclose(
        high_value, low_value, rel_tol=1e-8, abs_tol=1e-8,
    )


def flatten_single_talent_damage_variants(base_high, base_low, variants):
    """Flatten paired SimC facts without calculating damage or percentages in Django."""
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
    for identity in dict.fromkeys([*base_high_actions, *base_low_actions]):
        action = base_high_actions.get(identity) or base_low_actions[identity]
        baseline = action.get('baseline')
        if not isinstance(baseline, dict) or baseline.get('unresolved_reason'):
            continue
        row = copy.deepcopy(action)
        row['scenarios'] = []
        row['variant'] = {
            'talent_id': None, 'talent_name': '', 'talent_name_zh': '',
            'tree_type': '', 'trait_entry_id': None,
            'runtime_condition': (
                '无单项增伤天赋' if identity in base_high_actions
                else '目标生命值低于 35%'
            ),
            'reference_available': True,
        }
        rows.append(row)

    for item in variants:
        talent = item.get('talent') or {}
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
            base_high_action = base_high_actions.get(identity)
            base_low_action = base_low_actions.get(identity)
            high_amount = high_action.get('baseline') if high_action else None
            low_amount = low_action.get('baseline') if low_action else None
            base_high_amount = base_high_action.get('baseline') if base_high_action else None
            base_low_amount = base_low_action.get('baseline') if base_low_action else None
            candidates = []
            if _amount_expected(high_amount) is not None and _effect_changed(base_high_amount, high_amount):
                candidates.append((0, '选择该天赋后常驻生效', high_amount, base_high_amount))

            base_high_scenarios = {
                _scenario_tokens(scenario): scenario.get('values') or scenario.get('amount')
                for scenario in ((base_high_action or {}).get('scenarios') or [])
                if isinstance(scenario, dict)
            }
            high_scenarios = {
                _scenario_tokens(scenario): scenario.get('values') or scenario.get('amount')
                for scenario in ((high_action or {}).get('scenarios') or [])
                if isinstance(scenario, dict) and _scenario_tokens(scenario)
            }
            base_low_scenarios = {
                _scenario_tokens(scenario): scenario.get('values') or scenario.get('amount')
                for scenario in ((base_low_action or {}).get('scenarios') or [])
                if isinstance(scenario, dict)
            }
            low_scenarios = {
                _scenario_tokens(scenario): scenario.get('values') or scenario.get('amount')
                for scenario in ((low_action or {}).get('scenarios') or [])
                if isinstance(scenario, dict) and _scenario_tokens(scenario)
            }
            for tokens, amount in high_scenarios.items():
                if tokens in base_high_scenarios or tokens in base_low_scenarios:
                    continue
                reference = base_high_scenarios.get(tokens, base_high_amount)
                if _amount_expected(amount) is not None and _effect_changed(reference, amount):
                    candidates.append((1, f'需要 {" + ".join(tokens)} buff 激活', amount, reference))

            # Low health is classified from the difference between paired talent
            # effects. A baseline health change alone is not a talent condition.
            if _amount_expected(low_amount) is not None and _paired_effect_changed(
                base_high_amount, high_amount, base_low_amount, low_amount,
            ):
                candidates.append((2, '目标生命值低于 35%', low_amount, base_low_amount))
            for tokens, amount in low_scenarios.items():
                if tokens in base_high_scenarios or tokens in base_low_scenarios:
                    continue
                low_reference = base_low_scenarios.get(tokens, base_low_amount)
                high_current = high_scenarios.get(tokens, high_amount)
                high_reference = base_high_scenarios.get(tokens, base_high_amount)
                if _amount_expected(amount) is not None and _paired_effect_changed(
                    high_reference, high_current, low_reference, amount,
                ):
                    candidates.append((
                        3, f'目标生命值低于 35% + 需要 {" + ".join(tokens)} buff 激活',
                        amount, low_reference,
                    ))
                elif (_amount_expected(amount) is not None and tokens not in high_scenarios
                      and _effect_changed(low_reference, amount)):
                    candidates.append((1, f'需要 {" + ".join(tokens)} buff 激活', amount, low_reference))
            if not candidates:
                continue

            def delta(candidate):
                current = _amount_expected(candidate[2])
                reference = _amount_expected(candidate[3])
                # An absent reference stays absent; it is never converted to zero.
                return abs(current - reference) if current is not None and reference is not None else 0.0

            _priority, condition, selected_amount, comparison = max(
                candidates, key=lambda candidate: (candidate[0], delta(candidate)),
            )
            row = copy.deepcopy(high_action or low_action)
            row['baseline'] = copy.deepcopy(selected_amount)
            row['scenarios'] = []
            row['variant'] = {
                'talent_id': talent.get('id'),
                'talent_name': str(talent.get('name') or ''),
                'talent_name_zh': str(talent.get('name_zh') or ''),
                'tree_type': str(talent.get('tree_type') or ''),
                'trait_entry_id': talent.get('node_id'),
                'runtime_condition': condition,
                'reference_available': _amount_expected(comparison) is not None,
            }
            if row['variant']['reference_available'] is False:
                row['variant']['reference_unavailable_reason'] = 'action_absent_in_reference_actor'
            rows.append(row)
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
    DATASET_SCHEMA_REVISION = 4
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
        class_name = str(getattr(profile, 'class_name', '') or '').strip()
        spec_name = str(getattr(profile, 'spec', '') or '').strip()
        prefix = f'{class_name}_'.lower()
        if spec_name.lower().startswith(prefix):
            spec_name = spec_name[len(prefix):]
        rows = WowTalentNodeMetadata.objects.filter(
            talent_version__is_active=True,
            class_name__iexact=class_name,
            spec_name__iexact=spec_name,
            tree_type__in=('class', 'spec', 'hero'),
            node_id__isnull=False,
        ).order_by('tree_type', 'row', 'column', 'node_id', 'id')
        selected = []
        seen = set()
        for row in rows:
            key = (str(row.tree_type or '').lower(), row.node_id)
            if not isinstance(row.node_id, int) or row.node_id <= 0 or key in seen:
                continue
            seen.add(key)
            selected.append(row)
        if not selected:
            raise ValueError(f'{profile.spec} 缺少当前版本单项天赋 trait entry。')
        return selected

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

    def _binary_path(self):
        config = getattr(settings, 'SIMC_CONFIG', {}) or {}
        configured = str(config.get('simc_path') or '')
        path = str(configured or getattr(self.backend, 'simc_path', '') or '')
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise ValueError('SimC exporter 二进制不可执行。')
        return path

    def _run_profile_export(self, profile, talents, *, scaffold_talents=(), target_health=100):
        baseline_profile = copy.copy(profile)
        baseline_profile.talent = ''
        reference_input = SimcComposer(None).compose_validation_input(baseline_profile, '')
        simc_input = build_single_talent_actor_input(
            reference_input, profile.class_name, talents,
            scaffold_talents=scaffold_talents,
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
            for profile in self._profiles():
                all_talents = self._talent_entries(profile)
                scaffold_talents = self._spec_root_scaffold(all_talents)
                # A root remains an explicit possible variant even when it is
                # also selected to initialize the baseline actor's actions.
                talents = all_talents
                try:
                    high_export = self._run_profile_export(
                        profile, talents, scaffold_talents=scaffold_talents, target_health=100,
                    )
                    low_export = self._run_profile_export(
                        profile, talents, scaffold_talents=scaffold_talents, target_health=34,
                    )
                except RuntimeError as exc:
                    unresolved.append({
                        'specialization': str(profile.spec or ''),
                        'reason': str(exc)[-2000:],
                    })
                    continue

                high_actors = {
                    str(actor.get('name') or ''): actor
                    for actor in (high_export.get('actors') or [])
                    if isinstance(actor, dict)
                }
                low_actors = {
                    str(actor.get('name') or ''): actor
                    for actor in (low_export.get('actors') or [])
                    if isinstance(actor, dict)
                }
                base_high = high_actors.get('skill_damage_base')
                base_low = low_actors.get('skill_damage_base')
                if not base_high or not base_low:
                    unresolved.append({
                        'specialization': str(profile.spec or ''),
                        'reason': 'SimC exporter 缺少单项天赋基线 actor。',
                    })
                    continue

                variants = []
                for talent in talents:
                    actor_name = f'skill_damage_talent_{talent.pk}'
                    high_actor = high_actors.get(actor_name)
                    low_actor = low_actors.get(actor_name)
                    if not high_actor or not low_actor:
                        unresolved.append({
                            'specialization': str(profile.spec or ''),
                            'talent_metadata_id': talent.pk,
                            'trait_entry_id': talent.node_id,
                            'reason': 'SimC exporter 缺少单项天赋 actor。',
                        })
                        continue
                    variants.append({
                        'talent': {
                            'id': talent.pk,
                            'node_id': talent.node_id,
                            'tree_type': talent.tree_type,
                            'name': talent.name,
                            'name_zh': talent.name_zh,
                            'description': talent.description,
                            'description_zh': talent.description_zh,
                        },
                        'high': high_actor,
                        'low': low_actor,
                    })

                actor = copy.deepcopy(base_high)
                actor.pop('name', None)
                if 'specialization' not in actor and actor.get('spec'):
                    actor['specialization'] = actor.pop('spec')
                actor['variant_model'] = 'single_talent_runtime'
                actor['base_damage_basis'] = 'dbc_spell_effect_ap_sp_coefficients_at_100'
                actor['actions'] = flatten_single_talent_damage_variants(base_high, base_low, variants)
                actors.append(actor)
                unresolved.extend(high_export.get('unresolved', []))
                unresolved.extend(low_export.get('unresolved', []))
            payload = {
                'identity': {
                    'simc_revision': self.snapshot.simc_revision,
                    'game_build': self.snapshot.game_build,
                    'schema_revision': self.snapshot.schema_revision,
                },
                'preset': dict(self.FIXED_PRESET),
                'actors': actors,
                'unresolved': unresolved,
            }
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
