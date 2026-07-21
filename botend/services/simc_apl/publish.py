"""Persisted APL publication state and authoritative final-gate helpers."""
import hashlib
import platform as py_platform

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botend.models import SimcApl, SimcAplSymbol, SimcBackendBinary, SimcProfile
from botend.services.simc_apl.authoritative_validator import RestrictedSimcValidator
from botend.services.simc_apl.validation import validate_payload
from botend.services.simc_composer import SimcComposer


def content_hash(content):
    return hashlib.sha256(str(content or '').encode('utf-8')).hexdigest()


def current_validation_identity():
    configured = getattr(settings, 'SIMC_APL_CURRENT_IDENTITY', None)
    if configured and len(configured) == 2:
        return tuple(configured)
    platform = 'linuxarm64' if 'aarch64' in py_platform.machine().lower() else 'linux64'
    backend = SimcBackendBinary.objects.filter(platform=platform).first()
    if not backend or not backend.current_version:
        return None
    builds = list(SimcAplSymbol.objects.filter(
        is_active=True, simc_revision=backend.current_version,
    ).order_by().values_list('wow_build', flat=True).distinct()[:2])
    if len(builds) != 1:
        return None
    return backend.current_version, builds[0]


def validate_apl_for_profile(profile, apl):
    """Validate persisted APL content using the persisted Profile as authority."""
    identity = current_validation_identity()
    result = {
        'valid': False, 'content_hash': content_hash(apl.content),
        'revision': identity[0] if identity else '',
        'game_build': identity[1] if identity else '', 'diagnostics': [],
    }
    if not identity:
        result['error'] = 'validation_context_unavailable'
        return result
    platform = 'linuxarm64' if 'aarch64' in py_platform.machine().lower() else 'linux64'
    backend = SimcBackendBinary.objects.filter(platform=platform).first()
    if not backend:
        result['error'] = 'validation_backend_unavailable'
        return result
    try:
        validation_input = SimcComposer(profile.user_id).compose_validation_input(profile, apl.content)
        context = SimcComposer.validation_context(
            profile, catalog_revision=identity[0], binary_revision=backend.current_version,
            validation_input=validation_input,
        )
        validator = RestrictedSimcValidator(
            backend.simc_path, catalog_revision=identity[0],
            binary_revision=backend.current_version,
            temp_root=getattr(settings, 'SIMC_APL_VALIDATION_TEMP_ROOT', None),
        )
        payload = validate_payload(apl.content, mode='both',
                                   authoritative_validator=validator,
                                   validation_context=context)
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        result['error'] = 'validation_failed'
        result['diagnostics'] = [{'severity': 'error', 'message': str(exc)}]
        return result
    result['diagnostics'] = payload.get('diagnostics', [])
    result['valid'] = bool(payload.get('structural_valid') and payload.get('authoritative_valid'))
    result['details'] = payload
    return result


@transaction.atomic
def publish_apl(apl_id, user_id, profile_id):
    """Authoritatively validate and publish an exact persisted Profile/APL pair."""
    from botend.services.simc_player_config import canonical_simc_spec_identity
    from botend.services.simc_task_service import _build_profile_payload

    apl = SimcApl.objects.select_for_update().get(pk=apl_id)
    profile = SimcProfile.objects.select_for_update().get(
        pk=profile_id, user_id=user_id, is_active=True,
    )
    if apl.is_system or apl.owner_user_id != user_id:
        raise PermissionError('APL cannot be published by this user')
    profile_class, profile_spec = canonical_simc_spec_identity(profile.spec)
    apl_class, apl_spec = canonical_simc_spec_identity(apl.spec)
    if not profile_spec or profile_spec != apl_spec or (
        profile_class and apl_class and profile_class != apl_class
    ):
        raise ValueError('APL 专精与玩家配置专精不一致')

    before_hash = content_hash(apl.content)
    before_identity = current_validation_identity()
    profile_payload = _build_profile_payload(profile)
    result = validate_apl_for_profile(profile, apl)
    current = SimcApl.objects.select_for_update().get(pk=apl.pk)
    current_profile = SimcProfile.objects.select_for_update().get(pk=profile.pk)
    current_profile_payload = _build_profile_payload(current_profile)
    if (content_hash(current.content) != before_hash
            or current.spec != apl.spec
            or current_profile.spec != profile.spec
            or current_profile_payload != profile_payload
            or current_validation_identity() != before_identity):
        raise RuntimeError('APL or Profile changed during validation')

    if result.get('content_hash') != before_hash:
        raise RuntimeError('APL validation result does not match persisted content')
    exact_valid = bool(
        before_identity
        and result.get('valid')
        and result.get('content_hash') == before_hash
        and result.get('revision') == before_identity[0]
        and result.get('game_build') == before_identity[1]
    )
    current.validation_status = (
        SimcApl.VALIDATION_VALID if exact_valid else SimcApl.VALIDATION_INVALID
    )
    current.validated_content_hash = before_hash
    current.validation_revision = result.get('revision') or ''
    current.validation_game_build = result.get('game_build') or ''
    current.validation_diagnostics = result.get('diagnostics') or []
    current.validated_at = timezone.now()
    current.validation_stale_reason = ''
    current.is_selectable = exact_valid
    current.save(update_fields=[
        'validation_status', 'validated_content_hash', 'validation_revision',
        'validation_game_build', 'validation_diagnostics', 'validated_at',
        'validation_stale_reason', 'is_selectable',
    ])
    result['valid'] = exact_valid
    return result
