"""Trusted, two-stage conditional approvals on existing central item metadata.

These write functions are operator/import boundaries, NOT request/Agent APIs.
Review records must come from independent human/managed-build review. A digest
or Agent telemetry alone is not an approval. No runtime path writes this store.
"""
from copy import deepcopy
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.fields.json import KeyTransform

from botend.models import WowItemSnapshot
from botend.services.wow_item_effect_activation_store import (
    META_KEY as ACTIVATION_KEY, BUILD_RE, activation_reference, select_activation,
)
from simc_equipment_conditional import digest, validate_contract

META_KEY = 'simc_conditional_contracts_v1'


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _key(*, game_build, is_ptr, identity, relation_hash):
    if (type(is_ptr) is not bool or not isinstance(game_build, str)
            or not BUILD_RE.fullmatch(game_build) or not isinstance(identity, dict)
            or set(identity) != {'binary_sha256', 'revision', 'dbc_build'}
            or identity['dbc_build'] != game_build or not _sha(identity['binary_sha256'])
            or not isinstance(identity['revision'], str)
            or not re.fullmatch('[0-9a-f]{40}', identity['revision']) or not _sha(relation_hash)):
        raise ValidationError('conditional 精确构建/分支/产物身份无效')
    return digest({'game_build': game_build, 'is_ptr': is_ptr,
                   'identity': identity, 'relation_hash': relation_hash})


def _review(review, kind, source_fact_hash):
    if (not isinstance(review, dict) or review.get('kind') != kind
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
            or not _sha(review.get('evidence_sha256'))
            or review.get('source_fact_hash') != source_fact_hash):
        raise ValidationError('conditional 缺少独立审核或来源 hash 不一致')


def _contract(policy, expectation, authorization=None):
    try:
        return validate_contract(policy, expectation, authorization=authorization,
                                 require_authorization=authorization is not None)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValidationError(str(exc)) from exc


def _central_bindings(expectation, items, is_ptr):
    refs = {}
    for target in expectation['targets']:
        item = items.get(target['item_id'])
        if item is None:
            raise ValidationError('conditional 载体不在中央目录')
        fact = select_activation((item.metadata or {}).get(ACTIVATION_KEY, {}),
            is_ptr=is_ptr, item_id=target['item_id'], game_build=target['game_build'])
        if not fact or not fact.get('source', {}).get('evidence'):
            raise ValidationError('conditional 缺少同构建/分支中央激活来源')
        ref = activation_reference(fact)
        if (any(target[k] != ref[k] for k in
                ('item_id', 'game_build', 'driver_spell_ids', 'fact_hash'))
                or sorted(target['required_bonus_ids']) != ref['required_bonus_ids']
                or not set(target['event_spell_ids']).issubset(ref['event_spell_ids'])
                or (item.slot_key and item.slot_key != target['slot'])):
            raise ValidationError('conditional 中央载体/activation 来源绑定不一致')
        refs[target['slot']] = ref
    return refs


@transaction.atomic
def import_reviewed_contract(*, policy, expectation, is_ptr, review):
    """Stage 1: immutable reviewed contract; NEVER approves any executable.

    targets.fact_hash must reference activation_reference(existing central fact),
    not a crafting report hash. Source provenance remains unchanged.
    """
    policy, expectation, review = deepcopy((policy, expectation, review))
    relation = _contract(policy, expectation)
    _review(review, 'independent_source_review', relation['source_fact']['fact_hash'])
    identity = expectation['identity']
    key = _key(game_build=identity['dbc_build'], is_ptr=is_ptr, identity=identity,
               relation_hash=relation['relation_hash'])
    items = {i.item_id: i for i in WowItemSnapshot.objects.select_for_update().filter(
        item_id__in=[t['item_id'] for t in expectation['targets']]).order_by('item_id')}
    bindings = _central_bindings(expectation, items, is_ptr)
    owner_id = next(t['item_id'] for t in expectation['targets'] if t['role'] == 'changed')
    owner = items[owner_id]
    record = {'schema_version': 1, 'game_build': identity['dbc_build'], 'is_ptr': is_ptr,
              'owner_item_id': owner_id, 'policy': policy, 'expectation': expectation,
              'source_review': review, 'activation_references': bindings}
    metadata = deepcopy(owner.metadata or {})
    entries = metadata.setdefault(META_KEY, {})
    old = entries.get(key)
    if old is not None:
        if {k: v for k, v in old.items() if k != 'executables'} != record:
            raise ValidationError('conditional 已审核版本不可覆盖')
        return key
    entries[key] = {**record, 'executables': {}}
    owner.metadata = metadata
    owner.save(update_fields=['metadata'])
    return key


def _select(entries, *, owner_item_id, game_build, is_ptr, identity, relation_hash,
            source_fact_hash, platform):
    key = _key(game_build=game_build, is_ptr=is_ptr, identity=identity,
               relation_hash=relation_hash)
    if platform not in ('linux', 'windows') or not _sha(source_fact_hash):
        raise ValidationError('conditional 平台或来源 hash 无效')
    record = entries.get(key) if isinstance(entries, dict) else None
    if (not record or record.get('schema_version') != 1
            or record.get('owner_item_id') != owner_item_id or record.get('game_build') != game_build
            or type(record.get('is_ptr')) is not bool or record['is_ptr'] != is_ptr):
        raise ValidationError('conditional 未知 relation 或无精确构建/分支/产物版本')
    relation = _contract(record['policy'], record['expectation'])
    if (record['expectation']['identity'] != identity or relation['relation_hash'] != relation_hash
            or relation['source_fact']['fact_hash'] != source_fact_hash):
        raise ValidationError('conditional 已审核来源/身份不匹配')
    _review(record['source_review'], 'independent_source_review', source_fact_hash)
    return record


@transaction.atomic
def approve_executable(*, owner_item_id, game_build, is_ptr, identity, relation_hash,
                       source_fact_hash, platform, approval):
    """Stage 2: explicitly reviewed exact artifact, not Agent self-attestation.

    Windows requires its own contract with matching identity AND provenance;
    this function never rebinds Linux provenance or synthesizes relation hashes.
    """
    try:
        owner = WowItemSnapshot.objects.select_for_update().get(item_id=owner_item_id)
    except WowItemSnapshot.DoesNotExist as exc:
        raise ValidationError('conditional 中央载体不存在') from exc
    metadata = deepcopy(owner.metadata or {})
    record = _select(metadata.get(META_KEY), owner_item_id=owner_item_id, game_build=game_build,
                     is_ptr=is_ptr, identity=identity, relation_hash=relation_hash,
                     source_fact_hash=source_fact_hash, platform=platform)
    approval = deepcopy(approval)
    _review(approval, 'independent_executable_review', source_fact_hash)
    if (approval.get('platform') != platform or approval.get('identity') != identity
            or approval.get('relation_hashes') != [relation_hash]):
        raise ValidationError('conditional executable 审核范围不匹配')
    authorization = {k: deepcopy(approval[k]) for k in ('identity', 'relation_hashes')}
    _contract(record['policy'], record['expectation'], authorization)
    items = {i.item_id: i for i in WowItemSnapshot.objects.filter(
        item_id__in=[t['item_id'] for t in record['expectation']['targets']])}
    if _central_bindings(record['expectation'], items, is_ptr) != record['activation_references']:
        raise ValidationError('conditional 审核期间中央激活来源变化')
    previous = record['executables'].get(platform)
    if previous is not None and previous != approval:
        raise ValidationError('conditional executable 审核不可覆盖')
    record['executables'][platform] = approval
    owner.metadata = metadata
    owner.save(update_fields=['metadata'])
    return deepcopy(authorization)


def freeze_conditional_contract(*, owner_item_id, game_build, is_ptr, identity,
                                relation_hash, source_fact_hash, platform):
    """Read-only exact selection; returns a detached JSON-ready frozen triple.

    The caller supplies selectors, NEVER authorization/policy/expectation. Persist
    the result server-side once; runtime/completion must not re-query live facts.
    """
    entries = WowItemSnapshot.objects.filter(item_id=owner_item_id).values_list(
        KeyTransform(META_KEY, 'metadata'), flat=True).first()
    record = _select(entries, owner_item_id=owner_item_id, game_build=game_build,
                     is_ptr=is_ptr, identity=identity, relation_hash=relation_hash,
                     source_fact_hash=source_fact_hash, platform=platform)
    return _freeze_record(record, identity=identity, relation_hash=relation_hash,
                          source_fact_hash=source_fact_hash, platform=platform)


def freeze_referenced_conditional_contract(*, owner_item_id, contract_key):
    """Resolve public intent by reference, never accept a client contract.

    Planning reads independently reviewed source facts. Executable approval is
    still checked against the actual Agent at claim; this reader cannot grant it.
    """
    if (type(owner_item_id) is not int or owner_item_id <= 0
            or not isinstance(contract_key, str)
            or not re.fullmatch('[0-9a-f]{64}', contract_key)):
        raise ValidationError('conditional 无效中央契约引用')
    entries = WowItemSnapshot.objects.filter(item_id=owner_item_id).values_list(
        KeyTransform(META_KEY, 'metadata'), flat=True).first()
    record = entries.get(contract_key) if isinstance(entries, dict) else None
    if not isinstance(record, dict):
        raise ValidationError('conditional 缺少中央 source contract')
    relation = _contract(record.get('policy'), record.get('expectation'))
    identity = record['expectation']['identity']
    expected_key = _key(game_build=identity['dbc_build'], is_ptr=record.get('is_ptr'),
                        identity=identity, relation_hash=relation['relation_hash'])
    changed_id = next(t['item_id'] for t in record['expectation']['targets'] if t['role'] == 'changed')
    if (expected_key != contract_key or record.get('schema_version') != 1
            or record.get('game_build') != identity['dbc_build']
            or record.get('owner_item_id') != owner_item_id or changed_id != owner_item_id):
        raise ValidationError('conditional 中央契约引用不匹配')
    _review(record.get('source_review'), 'independent_source_review',
            relation['source_fact']['fact_hash'])
    items = {i.item_id: i for i in WowItemSnapshot.objects.filter(
        item_id__in=[t['item_id'] for t in record['expectation']['targets']])}
    if _central_bindings(record['expectation'], items, record['is_ptr']) != record.get('activation_references'):
        raise ValidationError('conditional 中央激活来源变化')
    return deepcopy({'policy': record['policy'], 'expectation': record['expectation'],
                     'is_ptr': record['is_ptr']})


def freeze_observed_conditional_contract(*, owner_item_id, game_build, is_ptr,
        observation, relation_hash, source_fact_hash, platform):
    """Select a UNIQUE two-stage approval, never fill telemetry from intent.

    One projected owner metadata read; no runtime writes. Revision is checked
    only after uniqueness, and trusted revision comes solely from the approval.
    """
    if (not isinstance(observation, dict)
            or set(observation) != {'binary_sha256', 'revision', 'dbc_build'}
            or not _sha(observation['binary_sha256'])
            or observation['dbc_build'] != game_build
            or not isinstance(game_build, str) or not BUILD_RE.fullmatch(game_build)
            or type(is_ptr) is not bool or platform not in ('linux', 'windows')
            or not _sha(relation_hash) or not _sha(source_fact_hash)
            or (observation['revision'] is not None and (
                not isinstance(observation['revision'], str)
                or not re.fullmatch('[0-9a-f]{40}', observation['revision'])))):
        raise ValidationError('conditional actual observation invalid')
    entries = WowItemSnapshot.objects.filter(item_id=owner_item_id).values_list(
        KeyTransform(META_KEY, 'metadata'), flat=True).first()
    matches = []
    for entry in (entries.values() if isinstance(entries, dict) else ()):
        try:
            identity = entry['expectation']['identity']
            if (identity['binary_sha256'] != observation['binary_sha256']
                    or identity['dbc_build'] != observation['dbc_build']
                    or entry['is_ptr'] != is_ptr or entry['owner_item_id'] != owner_item_id
                    or entry['expectation']['relations'][0]['relation_hash'] != relation_hash
                    or entry['expectation']['relations'][0]['source_fact']['fact_hash'] != source_fact_hash):
                continue
            key = _key(game_build=game_build, is_ptr=is_ptr, identity=identity,
                       relation_hash=relation_hash)
            record = _select({key: entry}, owner_item_id=owner_item_id, game_build=game_build,
                is_ptr=is_ptr, identity=identity, relation_hash=relation_hash,
                source_fact_hash=source_fact_hash, platform=platform)
            frozen = _freeze_record(record, identity=identity, relation_hash=relation_hash,
                source_fact_hash=source_fact_hash, platform=platform)
        except (ValidationError, KeyError, TypeError, AttributeError, IndexError):
            continue
        matches.append(frozen)
        if len(matches) > 1:
            raise ValidationError('conditional actual artifact approval ambiguous')
    if len(matches) != 1:
        raise ValidationError('conditional actual artifact approval missing')
    frozen = matches[0]
    if (observation['revision'] is not None
            and observation['revision'] != frozen['conditional_authorization']['identity']['revision']):
        raise ValidationError('conditional measured revision conflicts with approval')
    return frozen


def _freeze_record(record, *, identity, relation_hash, source_fact_hash, platform):
    approval = record['executables'].get(platform)
    if approval is None:
        raise ValidationError('conditional 无对应实际 binary 批准')
    _review(approval, 'independent_executable_review', source_fact_hash)
    if (approval.get('platform') != platform or approval.get('identity') != identity
            or approval.get('relation_hashes') != [relation_hash]):
        raise ValidationError('conditional 无对应实际 binary 批准')
    # Copy pins from the independent approved fact, NOT the contract/marker.
    authorization = {k: deepcopy(approval[k]) for k in ('identity', 'relation_hashes')}
    _contract(record['policy'], record['expectation'], authorization)
    return deepcopy({'policy': record['policy'], 'expectation': record['expectation'],
                     'conditional_authorization': authorization})
