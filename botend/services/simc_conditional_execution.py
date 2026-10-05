"""Server-only conditional selection and frozen Run reader.

Candidate fields are selectors/intent, never approval. Only the independent
central store supplies the triple. Telemetry selects an artifact, not authority.
No import/approval writes occur here; no fallback to Backend or marker identity.
"""
from copy import deepcopy
import re

from django.core.exceptions import ValidationError
from botend.models import SimulationRun
from botend.services.simc_conditional_store import freeze_observed_conditional_contract
from simc_equipment_conditional import validate_contract, digest

MANIFEST_KEY = 'conditional_execution'
CONTEXT_KEY = 'conditional_input_context_sha256'


def input_context_digest(code):
    """Bind actor/APL/options to claim; only gear and comments may change."""
    from simc_equipment_control import ALL_SLOTS, ALIASES
    lines = []
    for line in code.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, _ = line.partition('=')
        if sep and ALIASES.get(key.strip(), key.strip()) in ALL_SLOTS:
            continue
        lines.append(line)
    return digest(lines)


def validate_prepared_context(run, prepared_input):
    expected = (run.resource_manifest or {}).get(CONTEXT_KEY)
    if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
            or not isinstance(prepared_input, str)
            or input_context_digest(prepared_input) != expected):
        raise ValidationError('conditional prepared context differs from claim')


def is_conditional_candidate(params):
    params = params if isinstance(params, dict) else {}
    policy = params.get('equipment_effect_policy')
    expectation = params.get('equipment_effect_expectation')
    return (isinstance(policy, dict) and policy.get('version') == 3
            or isinstance(expectation, dict) and expectation.get('schema_version') == 2)


def _agent_identity(agent):
    caps = agent.capabilities if isinstance(agent.capabilities, dict) else {}
    if type(caps.get('conditional_evidence_protocol_version')) is not int or caps['conditional_evidence_protocol_version'] != 1:
        raise ValidationError('conditional evidence protocol unsupported')
    identity = {'binary_sha256': caps.get('binary_sha256'),
                'revision': caps.get('binary_revision'), 'dbc_build': caps.get('dbc_build')}
    if (caps.get('binary_identity_status') != 'ok'
            or not isinstance(identity['binary_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', identity['binary_sha256'])
            or (identity['revision'] is not None and (
                not isinstance(identity['revision'], str)
                or not re.fullmatch('[0-9a-f]{40}', identity['revision'])))
            or agent.platform not in ('linux', 'windows')):
        raise ValidationError('conditional actual Agent identity unavailable')
    return identity


def freeze_for_agent(run, *, agent, is_ptr):
    """Called under Task -> Run -> Agent locks; caller persists returned manifest."""
    params = run.candidate_params
    if not is_conditional_candidate(params):
        if MANIFEST_KEY in (run.resource_manifest or {}):
            raise ValidationError('conditional frozen candidate changed')
        return None
    identity = _agent_identity(agent)
    if MANIFEST_KEY in (run.resource_manifest or {}):
        frozen = _validated(run)
        if (frozen['actual_observation'] != identity
                or frozen['platform'] != agent.platform or frozen['is_ptr'] != is_ptr):
            raise ValidationError('conditional frozen execution identity changed')
        return frozen
    try:
        # Untrusted selectors are only used for exact lookup in the approved store.
        relation = validate_contract(params['equipment_effect_policy'],
            params['equipment_effect_expectation'], require_authorization=False)
        owner = next(t['item_id'] for t in params['equipment_effect_expectation']['targets']
                     if t['role'] == 'changed')
        frozen = freeze_observed_conditional_contract(owner_item_id=owner,
            game_build=identity['dbc_build'], is_ptr=is_ptr, observation=identity,
            platform=agent.platform, relation_hash=relation['relation_hash'],
            source_fact_hash=relation['source_fact']['fact_hash'])
        if (frozen['policy'] != params['equipment_effect_policy']
                or frozen['expectation'] != params['equipment_effect_expectation']):
            raise ValidationError('conditional candidate differs from approved contract')
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise ValidationError('conditional invalid selector/contract') from exc
    return {**frozen, 'platform': agent.platform, 'is_ptr': is_ptr,
            'actual_observation': deepcopy(identity),
            'candidate_hash': digest(params)}


def _validated(run):
    frozen = (run.resource_manifest or {}).get(MANIFEST_KEY)
    conditional = is_conditional_candidate(run.candidate_params)
    if frozen is None and not conditional:
        return None
    if not isinstance(frozen, dict) or not conditional:
        raise ValidationError('conditional server frozen authorization missing')
    try:
        if (set(frozen) != {'policy', 'expectation', 'conditional_authorization',
                            'platform', 'is_ptr', 'candidate_hash', 'actual_observation'}
                or frozen['candidate_hash'] != digest(run.candidate_params)
                or frozen['policy'] != run.candidate_params['equipment_effect_policy']
                or frozen['expectation'] != run.candidate_params['equipment_effect_expectation']
                or frozen['platform'] not in ('linux', 'windows')
                or type(frozen['is_ptr']) is not bool):
            raise ValueError('binding')
        validate_contract(frozen['policy'], frozen['expectation'],
                          authorization=frozen['conditional_authorization'])
        actual = frozen['actual_observation']
        approved = frozen['conditional_authorization']['identity']
        if (not isinstance(actual, dict) or set(actual) != set(approved)
                or actual['binary_sha256'] != approved['binary_sha256']
                or actual['dbc_build'] != approved['dbc_build']
                or (actual['revision'] is not None and actual['revision'] != approved['revision'])):
            raise ValueError('observation binding')
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError('conditional server frozen authorization invalid') from exc
    return deepcopy(frozen)


def read_frozen_conditional_execution(run_id):
    """Strict completion/Worker reader: DB Run only, never metadata/report/marker.

    Returns None only for ordinary runs. Missing conditional freeze raises.
    No live central lookup: historical execution remains pinned after revocation.
    """
    run = SimulationRun.objects.only('candidate_params', 'resource_manifest').get(pk=run_id)
    return _validated(run)
