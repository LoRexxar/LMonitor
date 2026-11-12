"""Write-maintained, disposable ranking index. Never called by a GET.

Bulk/QuerySet resource writes bypass signals: callers must refresh_configs(ids).
QuerySet completion writes must call sync_result(id) in the completing transaction.
Lock order is config -> central settings -> reference results -> ranking entries.
"""
import math

from django.db import transaction
from django.db.models import Q
from django.db.models.signals import pre_save, post_save, pre_delete, post_delete, m2m_changed
from django.utils import timezone

from botend.models import (
    SimcApl, SimcBackendBinary, SimcContentTemplate, SimcProfile, SimcTalentString,
    SimcReferencePublication, SimcReferenceRankingEntry as Entry,
    SimcReferenceSimulation as Result, SimcReferenceSimulationConfig as Config,
    SimcReferenceSimulationSettings as Settings, SimcReferenceSimulationScenario as Scenario,
)
from botend.services import simc_reference_simulations as source
from botend.services.simc_task_service import _compute_content_hash
from botend.services.simc_player_config import canonical_simc_profile_identity


def comparison_key(config, params):
    """Comparable full conditions, without binary/build/Git identity."""
    return _compute_content_hash({'params': params, 'use_ptr': config.use_ptr,
                                 'backend': [config.backend_id], 'template': config.template.content})


def _locked_config(config_id):
    config = Config.objects.select_for_update().get(pk=config_id)
    config.settings = Settings.objects.select_for_update().get(pk=config.settings_id)
    return config


def _scenes(config):
    grouped = {}
    for scene in source.active_scenarios(config):
        try:
            key = _compute_content_hash(source.scenario_simulation_params(scene))
        except ValueError:
            continue
        grouped.setdefault(key, []).append(scene)
    return {key: rows[0] for key, rows in grouped.items() if len(rows) == 1}


def _published(config):
    publication = SimcReferencePublication.objects.filter(spec_key=config.spec_key, is_published=True).first()
    return {kind: set(getattr(publication, kind).values_list('pk', flat=True)) if publication else set()
            for kind in ('profiles', 'talents', 'apls')}


def _validate(row, config, scenes, published, cache):
    if (row.status != 'completed' or not isinstance(row.dps, (int, float)) or isinstance(row.dps, bool)
            or not math.isfinite(row.dps) or row.dps <= 0 or not isinstance(row.snapshot, dict)
            or not all((row.profile_id, row.talent_id, row.apl_id))):
        return None
    params = row.snapshot.get('simulation_params')
    if not isinstance(params, dict):
        return None
    scene = scenes.get(_compute_content_hash(params))
    if not scene or (row.scenario_id is not None and row.scenario_id != scene.pk):
        return None
    slot = (row.profile_id, row.talent_id, row.apl_id, scene.pk)
    if slot not in cache:
        try:
            frozen = source.snapshot(config, row.profile, row.talent, row.apl, scene, published_resources=published)
            class_key, _ = canonical_simc_profile_identity(row.profile.spec, row.profile.class_name)
            cache[slot] = (_compute_content_hash(frozen), class_key, comparison_key(config, params))
        except (ValueError, TypeError, KeyError):
            cache[slot] = None
    current = cache[slot]
    if not current or current[0] != _compute_content_hash(source.normalized_snapshot(row.snapshot)):
        return None
    signature, class_key, cohort = current
    return dict(config_id=config.pk, profile_id=row.profile_id, talent_id=row.talent_id, apl_id=row.apl_id,
                scenario_id=scene.pk, result_id=row.pk, class_key=class_key, spec_key=config.spec_key,
                input_signature=signature, comparison_key=cohort, is_current=True)


def _store(values, existing=None, *, protect_latest=False):
    slot = {key: values[key] for key in ('config_id', 'profile_id', 'talent_id', 'apl_id', 'scenario_id')}
    if existing is None:
        existing = Entry.objects.select_for_update().filter(**slot).first()
    if not existing:
        Entry.objects.create(**values)
        return True
    if (protect_latest and existing.result_id > values['result_id']
            and existing.input_signature == values['input_signature']):
        return False
    changed = {key: value for key, value in values.items() if getattr(existing, key) != value}
    if not changed:
        return False
    Entry.objects.filter(pk=existing.pk).update(**changed, updated_at=timezone.now())
    return True


def sync_result(result_id):
    """Validate this result alone. Failed/pending reruns never replace success."""
    config_id = Result.objects.filter(pk=result_id).values_list('config_id', flat=True).first()
    if config_id is None:
        return False
    with transaction.atomic():
        config = _locked_config(config_id)
        row = Result.objects.select_for_update().get(pk=result_id)
        values = _validate(row, config, _scenes(config), _published(config), {})
        if values is None:
            Entry.objects.filter(result_id=result_id, is_current=True).update(is_current=False, updated_at=timezone.now())
            return False
        return _store(values, protect_latest=True)


def refresh_configs(config_ids=None):
    """Idempotently rebuild each selected config from latest valid successes.

    Scan successful history once per config, memoizing current full-input hashes per
    resource/scenario slot. Legacy backend version tails are normalized only in memory.
    No history, frozen signature, task, or simulation is rewritten or created.
    """
    configs = Config.objects.order_by('pk')
    if config_ids is not None:
        configs = configs.filter(pk__in=config_ids)
    stats = {'configs': 0, 'changed': 0, 'invalidated': 0}
    for config_id in configs.values_list('pk', flat=True).iterator(chunk_size=100):
        with transaction.atomic():
            config = _locked_config(config_id)
            scenes, published, cache, selected = _scenes(config), _published(config), {}, {}
            # Serialize references before entries; only successful history is relevant.
            rows = Result.objects.select_for_update().filter(config_id=config_id, status='completed', dps__gt=0).order_by('-pk')
            for row in rows.iterator(chunk_size=200):
                if (row.profile_id, row.talent_id, row.apl_id, row.scenario_id) in selected:
                    continue
                values = _validate(row, config, scenes, published, cache)
                if values is not None:
                    slot = tuple(values[k] for k in ('profile_id', 'talent_id', 'apl_id', 'scenario_id'))
                    selected.setdefault(slot, values)
            existing = { (e.profile_id, e.talent_id, e.apl_id, e.scenario_id): e
                         for e in Entry.objects.select_for_update().filter(config_id=config_id) }
            for slot, values in selected.items():
                stats['changed'] += int(_store(values, existing.get(slot)))
            obsolete = [entry.pk for slot, entry in existing.items() if slot not in selected and entry.is_current]
            if obsolete:
                stats['invalidated'] += Entry.objects.filter(pk__in=obsolete).update(is_current=False, updated_at=timezone.now())
            stats['configs'] += 1
    return stats


def _affected(instance):
    if isinstance(instance, Config):
        return Config.objects.filter(pk=instance.pk)
    if isinstance(instance, Settings):
        return Config.objects.filter(settings_id=instance.pk)
    if isinstance(instance, Scenario):
        return Config.objects.filter(settings_id=instance.settings_id)
    if isinstance(instance, SimcReferencePublication):
        return Config.objects.filter(spec_key=instance.spec_key)
    if isinstance(instance, SimcContentTemplate):
        return Config.objects.filter(settings__template_id=instance.pk)
    if isinstance(instance, SimcBackendBinary):
        return Config.objects.filter(settings__backend_id=instance.pk)
    field = {SimcProfile: 'profile', SimcTalentString: 'talent', SimcApl: 'apl'}[type(instance)]
    publication_ids = SimcReferencePublication.objects.filter(**{field + 's': instance.pk}).values('spec_key')
    return Config.objects.filter(Q(spec_key__in=publication_ids) | Q(**{'simulations__' + field + '_id': instance.pk})).distinct()


def _ids(instance):
    return set(_affected(instance).values_list('pk', flat=True))


def invalidate_configs(config_ids):
    """Bulk-write hook: invalidate now, refresh frozen targets after commit.

    Call inside the resource mutation transaction. Empty input performs no SQL
    and schedules no callback. IDs are captured eagerly, not as a lazy QuerySet.
    """
    ids = tuple(sorted(set(config_ids)))
    # Invalidate in the write transaction; rebuild from authoritative inputs only
    # after commit. Rollbacks also roll back invalidation and discard the callback.
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        Entry.objects.filter(config_id__in=batch, is_current=True).update(is_current=False, updated_at=timezone.now())
        transaction.on_commit(lambda batch=batch: refresh_configs(batch))


def _before(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk:
        return
    previous = sender.objects.filter(pk=instance.pk).first()
    instance._ranking_previous_configs = _ids(previous) if previous else set()
    if sender is SimcBackendBinary:
        instance._ranking_previous_active = previous.is_active if previous else None


def _changed(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    if update_fields is not None and set(update_fields) <= {'updated_at', 'last_used_at'}:
        return
    # SimC maintenance metadata never makes a current ranking stale, including
    # ordinary save() calls with no explicit update_fields.
    if sender is SimcBackendBinary:
        if update_fields is not None and 'is_active' not in update_fields:
            return
        if getattr(instance, '_ranking_previous_active', None) == instance.is_active:
            return
    invalidate_configs(_ids(instance) | getattr(instance, '_ranking_previous_configs', set()))


def _deleted(sender, instance, **kwargs):
    invalidate_configs(getattr(instance, '_ranking_previous_configs', set()))


def _result_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        transaction.on_commit(lambda pk=instance.pk: sync_result(pk))


def _publication_m2m(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action.startswith('pre_'):
        if reverse:
            ids = _ids(instance)
            # pre_add does not yet appear in the reverse relation.
            if pk_set:
                specs = SimcReferencePublication.objects.filter(pk__in=pk_set).values('spec_key')
                ids |= set(Config.objects.filter(spec_key__in=specs).values_list('pk', flat=True))
        else:
            ids = _ids(instance)
        instance._ranking_m2m_configs = ids
    elif action.startswith('post_'):
        invalidate_configs(getattr(instance, '_ranking_m2m_configs', set()))


def register_signals():
    """Idempotent registry; called by AppConfig.ready, not during GET."""
    models = (Config, Settings, Scenario, SimcReferencePublication, SimcProfile,
              SimcTalentString, SimcApl, SimcContentTemplate, SimcBackendBinary)
    for model in models:
        label = model._meta.label_lower
        pre_save.connect(_before, sender=model, dispatch_uid='ranking_before_' + label)
        pre_delete.connect(_before, sender=model, dispatch_uid='ranking_delete_before_' + label)
        post_save.connect(_changed, sender=model, dispatch_uid='ranking_after_' + label)
        post_delete.connect(_deleted, sender=model, dispatch_uid='ranking_delete_' + label)
    post_save.connect(_result_changed, sender=Result, dispatch_uid='ranking_result')
    for field in ('profiles', 'talents', 'apls'):
        through = getattr(SimcReferencePublication, field).through
        m2m_changed.connect(_publication_m2m, sender=through, dispatch_uid='ranking_m2m_' + field)
