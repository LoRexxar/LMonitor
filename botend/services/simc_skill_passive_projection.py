"""Report native passive candidates without inventing application provenance.

DBC relationships and parser/base numerical consistency do not prove that a
source survives derived-action construction. Until the exporter provides an
independent initialization-time application trace/counterfactual, none of those
candidates can authorize division or an ``excluded_from_action_damage`` claim.
This boundary deliberately leaves every amount, factor and source row intact.
"""
import math


_CONSISTENCY_METHOD = 'registered_passive_base_consistency_v1'
_CONSISTENCY_FIELDS = {
    'method', 'action_spell_id', 'layer', 'parser_multiplier',
    'base_multiplier', 'runtime_multiplier', 'runtime_without_parser_multiplier',
}


def validate_passive_consistency(evidence, component_name, spell_id):
    """Validate diagnostic evidence only; success does NOT authorize division."""
    layer = 'da_multiplier' if component_name == 'direct' else 'ta_multiplier'
    if (not isinstance(evidence, dict) or set(evidence) != _CONSISTENCY_FIELDS
            or evidence.get('method') != _CONSISTENCY_METHOD
            or type(evidence.get('action_spell_id')) is not int
            or evidence['action_spell_id'] != spell_id
            or evidence.get('layer') != layer):
        raise ValueError('Invalid passive consistency identity')
    for field in _CONSISTENCY_FIELDS - {'method', 'action_spell_id', 'layer'}:
        value = evidence[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid passive consistency multiplier')
    if (not math.isclose(evidence['base_multiplier'], evidence['parser_multiplier'], rel_tol=1e-8)
            or not math.isclose(evidence['runtime_multiplier'],
                                evidence['runtime_without_parser_multiplier'] * evidence['parser_multiplier'],
                                rel_tol=1e-8)):
        raise ValueError('Inconsistent passive base probe')
    return evidence


def normalize_native_specialization_passives(actor, *, diagnostics=None):
    """Preserve unproven candidates and report why they were not normalized.

    The name preserves the projection boundary's caller contract. Its result is
    the set of safely extracted global effects: currently empty, not a claim
    that the actor has no specialization passives. Diagnostics are external to
    the input so frozen/native actors remain byte-for-byte equivalent as data.
    In particular, even a parser-consistent base can have overwritten provenance.
    """
    if not isinstance(actor, dict):
        return []
    if diagnostics is None:
        diagnostics = []
    for index, action in enumerate(actor.get('actions') or []):
        if not isinstance(action, dict):
            continue
        baseline = action.get('baseline')
        if not isinstance(baseline, dict):
            continue
        for component_name in ('direct', 'tick'):
            component = baseline.get(component_name)
            if not isinstance(component, dict):
                continue
            layers = component.get('runtime_layers')
            if not isinstance(layers, dict):
                continue
            candidates = layers.get('specialization_passive_effects') or []
            if not isinstance(candidates, list):
                candidates = [candidates]
            for candidate in candidates:
                reason = 'missing_runtime_application'
                if isinstance(candidate, dict) and candidate.get('parser_consistency') is not None:
                    try:
                        validate_passive_consistency(candidate['parser_consistency'], component_name,
                                                     action.get('spell_id'))
                        reason = 'parser_consistency_not_application'
                    except (ValueError, OverflowError):
                        reason = 'invalid_parser_consistency'
                elif isinstance(candidate, dict) and 'application' in candidate:
                    # Do not accept the superseded base-intervention experiment
                    # or arbitrary future claims under an unversioned flag.
                    reason = 'unsupported_application_evidence'
                diagnostics.append({
                    'reason': reason, 'action_index': index,
                    'spell_id': action.get('spell_id'), 'token': action.get('token'),
                    'component': component_name,
                    'source_spell_id': candidate.get('source_spell_id') if isinstance(candidate, dict) else None,
                    'effect_index': candidate.get('effect_index') if isinstance(candidate, dict) else None,
                })
    return []
