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


class SimcSkillDamageSnapshotService:
    """Generate one persisted exporter dataset for one SimC/DBC/schema identity."""

    EXPORTER_SCHEMA_REVISION = 2
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

    def _run_profile_export(self, profile, talent):
        baseline_profile = copy.copy(profile)
        baseline_profile.talent = talent.talent
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
        self._validate_export(payload)
        return payload

    def _validate_export(self, payload):
        if payload.get('schema_version') != self.snapshot.schema_revision:
            raise ValueError('exporter schema revision 不匹配。')
        if payload.get('simc_revision') != self.snapshot.simc_revision:
            raise ValueError('exporter SimC revision 不匹配。')
        if payload.get('game_build') != self.snapshot.game_build:
            raise ValueError('exporter game build 不匹配。')
        normalization = payload.get('normalization_basis') or {}
        if normalization != self.FIXED_PRESET:
            raise ValueError('exporter 未按 AP/SP=100、暴击=20%、精通=50% 的固定预制生成。')
        if not isinstance(payload.get('actors'), list):
            raise ValueError('exporter actors 结构无效。')
        required_amount_fields = ('hit', 'crit', 'crit_chance', 'expected')
        for actor in payload['actors']:
            if not isinstance(actor, dict) or not isinstance(actor.get('actions'), list):
                raise ValueError('exporter actor/actions 结构无效。')
            for action in actor['actions']:
                if not isinstance(action, dict):
                    raise ValueError('exporter action 结构无效。')
                if action.get('supported') is False:
                    if not action.get('unsupported_reason'):
                        raise ValueError('exporter unsupported action 缺少原因。')
                    continue
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
                for actor in exported.get('actors', []):
                    actor = dict(actor)
                    actor.pop('name', None)
                    if 'specialization' not in actor and actor.get('spec'):
                        actor['specialization'] = actor.pop('spec')
                    actor['hero_talent_tree'] = hero_tree
                    actor['talent_name'] = selected_talent.name
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
