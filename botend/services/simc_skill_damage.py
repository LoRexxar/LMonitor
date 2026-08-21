import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from botend.models import SimcBackendBinary, SimcProfile, SimcSkillDamageSnapshot
from botend.services.simc_composer import SimcComposer


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
        if latest and (
            latest.simc_revision == revision
            and latest.game_build == game_build
            and latest.schema_revision == cls.EXPORTER_SCHEMA_REVISION
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
        if not created and snapshot.status in (
            SimcSkillDamageSnapshot.STATUS_RUNNING,
            SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
        ):
            raise ValueError('该 SimC/DBC/exporter 版本已生成或正在生成。')
        if not created:
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

    def _binary_path(self):
        config = getattr(settings, 'SIMC_CONFIG', {}) or {}
        configured = str(config.get('simc_path') or '')
        path = str(configured or getattr(self.backend, 'simc_path', '') or '')
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise ValueError('SimC exporter 二进制不可执行。')
        return path

    def _run_profile_export(self, profile):
        simc_input = SimcComposer(None).compose_validation_input(profile, '')
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
            for profile in self._profiles():
                exported = self._run_profile_export(profile)
                for actor in exported.get('actors', []):
                    actor = dict(actor)
                    actor.pop('name', None)
                    if 'specialization' not in actor and actor.get('spec'):
                        actor['specialization'] = actor.pop('spec')
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
