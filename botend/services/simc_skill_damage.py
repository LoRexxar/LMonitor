import copy
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, models
from django.utils import timezone

from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, SimcBackendBinary, SimcProfile,
    SimcSkillDamageSnapshot, SimcTalentString, WowTalentNodeMetadata,
)
from botend.services.simc_composer import SimcComposer
from botend.services.simc_hero_talents import HeroTalentAnalysisError, resolve_hero_talent_names


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
            and str(actor.get('hero_talent_tree') or '').strip()
            and actor.get('action_universe') == 'dbc_spellbook_selected_traits_and_derived_actions'
            for actor in latest_actors
        )
        if latest and (
            latest.simc_revision == revision
            and latest.game_build == game_build
            and latest.schema_revision == cls.EXPORTER_SCHEMA_REVISION
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
            schema_revision=cls.EXPORTER_SCHEMA_REVISION,
            defaults={'requested_by_id': requested_by_id},
        )
        if not created:
            actors = (snapshot.payload or {}).get('actors') or []
            has_complete_actor_data = bool(actors) and all(
                isinstance(actor, dict)
                and str(actor.get('hero_talent_tree') or '').strip()
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

    def _talents(self):
        return list(SimcTalentString.objects.filter(
            is_active=True,
            is_selectable=True,
            is_system=True,
        ).exclude(talent='').order_by('spec', '-modified_at', '-id'))

    def _baselines(self):
        """Choose one server-owned talent build for every actual spec/hero-tree pair."""
        profiles = {str(row.spec or '').lower(): row for row in self._profiles()}
        grouped = {}
        for talent in self._talents():
            spec = str(talent.spec or '').lower()
            profile = profiles.get(spec)
            if not profile:
                continue
            rank = (
                str(getattr(talent, 'system_key', '') or '') == f'simc_upstream:{spec}',
                talent.modified_at,
                talent.pk,
            )
            try:
                names = resolve_hero_talent_names(talent.talent, spec)
            except HeroTalentAnalysisError:
                continue
            if len(names) != 1:
                continue
            hero_tree = str(names[0] or '').strip()
            if not hero_tree:
                continue
            grouped.setdefault((spec, hero_tree), []).append((rank, profile, talent, hero_tree))

        baselines = []
        self._baseline_fallback_map = {}
        for key, rows in grouped.items():
            rows.sort(key=lambda row: row[0], reverse=True)
            _rank, profile, selected_talent, hero_tree = rows[0]
            baselines.append((profile, selected_talent, hero_tree))
            self._baseline_fallback_map[key] = [row[2] for row in rows[1:]]
        baselines.sort(key=lambda row: (str(row[0].spec or '').lower(), row[2]))
        if not baselines:
            raise ValueError('没有可用于技能伤害快照的专精/英雄天赋树基线。')
        return baselines

    def _fallback_talents(self, profile, talent, hero_tree):
        key = (str(getattr(profile, 'spec', '') or '').lower(), str(hero_tree or '').strip())
        return [
            candidate for candidate in getattr(self, '_baseline_fallback_map', {}).get(key, [])
            if candidate.pk != talent.pk
        ]

    def _binary_path(self):
        config = getattr(settings, 'SIMC_CONFIG', {}) or {}
        configured = str(config.get('simc_path') or '')
        path = str(configured or getattr(self.backend, 'simc_path', '') or '')
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise ValueError('SimC exporter 二进制不可执行。')
        return path

    def _run_profile_export(self, profile, talent=None):
        baseline_profile = copy.copy(profile)
        baseline_profile.talent = talent.talent if talent is not None else ''
        simc_input = SimcComposer(None).compose_validation_input(baseline_profile, '')
        with tempfile.TemporaryDirectory(prefix='simc-skill-damage-') as tmp:
            input_path = Path(tmp) / 'actor.simc'
            output_path = Path(tmp) / 'export.json'
            input_path.write_text(simc_input, encoding='utf-8')
            command = [
                self._binary_path(), str(input_path),
                f'skill_damage_export={output_path}',
                f'skill_damage_revision={self.snapshot.simc_revision}',
                f'skill_damage_game_build={self.snapshot.game_build}',
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if result.returncode != 0 or not output_path.exists():
                diagnostic = (result.stderr or result.stdout or 'SimC exporter 未生成 JSON').strip()
                raise RuntimeError(diagnostic[-2000:])
            payload = json.loads(output_path.read_text(encoding='utf-8'))
        self._validate_export(payload, profile=profile)
        return payload

    def _validate_export(self, payload, *, profile=None):
        if payload.get('schema_version') != self.snapshot.schema_revision:
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
        if len(actors) != 1:
            raise ValueError('每次完整天赋 exporter 必须恰好一个 actor。')
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
            for profile, talent, hero_tree in self._baselines():
                exported = None
                selected_talent = None
                attempts = [talent, *self._fallback_talents(profile, talent, hero_tree)]
                errors = []
                seen_talent_ids = set()
                for candidate in attempts:
                    if candidate.pk in seen_talent_ids:
                        continue
                    seen_talent_ids.add(candidate.pk)
                    try:
                        exported = self._run_profile_export(profile, candidate)
                    except RuntimeError as exc:
                        errors.append((candidate.pk, str(exc)[-2000:]))
                        continue
                    selected_talent = candidate
                    break
                if exported is None:
                    unresolved.append({
                        'specialization': str(profile.spec or ''),
                        'hero_talent_tree': hero_tree,
                        'talent_id': talent.pk,
                        'reason': errors[-1][1] if errors else '没有可执行的同英雄树天赋候选。',
                    })
                    continue
                selected_actors = [
                    actor for actor in exported.get('actors', [])
                    if isinstance(actor, dict)
                ]
                for exported_actor in selected_actors:
                    actor = copy.deepcopy(exported_actor)
                    actor.pop('name', None)
                    if 'specialization' not in actor and actor.get('spec'):
                        actor['specialization'] = actor.pop('spec')
                    actor['hero_talent_tree'] = hero_tree
                    actor['talent_name'] = selected_talent.name
                    actor['base_damage_basis'] = 'dbc_spell_effect_ap_sp_coefficients_at_100'
                    attach_runtime_product_metrics(actor)
                    actors.append(actor)
                unresolved.extend(exported.get('unresolved', []))
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
