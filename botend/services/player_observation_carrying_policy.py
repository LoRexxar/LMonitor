"""Central carrying frequency policy; pure Python, safe for light frozen reads.

A frequency is scoped to ONE four-character core in the already-selected sample.
Unknown runs stay in observed_runs. This is not a complete activity history.
"""
from copy import deepcopy
import re

WINDOW_DAYS = 14
SAMPLE_LIMIT = 20
MIN_VERIFIED_RUNS = 6
MIN_DISTINCT_BENEFICIARIES = 2
POLICY_VERSION = 1


def _ids(values):
    return (isinstance(values, list) and all(type(v) is int and v > 0 for v in values)
            and len(values) == len(set(values)))


def _members(values):
    return (isinstance(values, list) and all(isinstance(v, str) and
            re.fullmatch(r'[0-9a-f]{64}', v) for v in values) and len(values) == len(set(values)))


def build_frequency_summary(*, selected_ids, events, unknown_ids, core_members, core_run_ids):
    observed = sorted(set(selected_ids))
    verified = sorted({e['run_id'] for e in events} & set(observed))
    unknown = sorted((set(unknown_ids) & set(observed)) - set(verified))
    beneficiaries = sorted({e['guest'] for e in events if e['run_id'] in verified})
    return dict(policy_version=POLICY_VERSION, window_days=WINDOW_DAYS, sample_limit=SAMPLE_LIMIT,
        min_verified_runs=MIN_VERIFIED_RUNS, min_distinct_beneficiaries=MIN_DISTINCT_BENEFICIARIES,
        observed_runs=len(observed), verified_runs=len(verified), unknown_runs=len(unknown),
        assessed_runs=len(observed)-len(unknown),
        not_supporting_runs=len(observed)-len(unknown)-len(verified),
        distinct_beneficiaries=len(beneficiaries), core_runs=len(set(core_run_ids)),
        observed_run_ids=observed, verified_run_ids=verified, unknown_run_ids=unknown,
        verified_evidence_ids=[f'behavior:carrying:{rid}' for rid in verified],
        core_members=list(core_members), beneficiary_keys=beneficiaries,
        supported=len(verified) >= MIN_VERIFIED_RUNS and len(beneficiaries) >= MIN_DISTINCT_BENEFICIARIES,
        legacy=False)


def validate_frequency_summary(summary):
    """Validate new explicit metadata, returning a detached copy or None.

    Callers must first validate frozen publication integrity/privacy. This does
    not re-derive source facts or grant publication permission by itself.
    """
    if not isinstance(summary, dict):
        return None
    constants = dict(policy_version=POLICY_VERSION, window_days=WINDOW_DAYS, sample_limit=SAMPLE_LIMIT,
        min_verified_runs=MIN_VERIFIED_RUNS, min_distinct_beneficiaries=MIN_DISTINCT_BENEFICIARIES)
    counts = ('observed_runs', 'verified_runs', 'unknown_runs', 'assessed_runs',
              'not_supporting_runs', 'distinct_beneficiaries', 'core_runs')
    if (any(type(summary.get(k)) is not int or summary[k] != v for k, v in constants.items())
            or any(type(summary.get(k)) is not int or summary[k] < 0 for k in counts)
            or type(summary.get('supported')) is not bool or summary.get('legacy') is not False):
        return None
    if any(not _ids(summary.get(k)) for k in ('observed_run_ids', 'verified_run_ids', 'unknown_run_ids')):
        return None
    if not _members(summary.get('core_members')) or not _members(summary.get('beneficiary_keys')):
        return None
    observed, verified, unknown = (set(summary[k]) for k in
                                  ('observed_run_ids', 'verified_run_ids', 'unknown_run_ids'))
    if summary.get('verified_evidence_ids') != [f'behavior:carrying:{rid}' for rid in sorted(verified)]:
        return None
    if (not verified <= observed or not unknown <= observed or verified & unknown
            or len(observed) > SAMPLE_LIMIT or len(observed) != summary['observed_runs']
            or len(verified) != summary['verified_runs'] or len(unknown) != summary['unknown_runs']
            or len(summary['beneficiary_keys']) != summary['distinct_beneficiaries']
            or summary['distinct_beneficiaries'] > len(verified)
            or summary['assessed_runs'] != len(observed)-len(unknown)
            or summary['not_supporting_runs'] != len(observed)-len(unknown)-len(verified)
            or not len(verified) <= summary['core_runs'] <= len(observed)
            or (verified and len(summary['core_members']) != 4)
            or set(summary['core_members']) & set(summary['beneficiary_keys'])):
        return None
    supported = len(verified) >= MIN_VERIFIED_RUNS and summary['distinct_beneficiaries'] >= MIN_DISTINCT_BENEFICIARIES
    if summary['supported'] is not supported:
        return None
    return deepcopy(summary)


def assess_carrying_frequency(review):
    """Read a full or light review (only carrying_frequency is needed for new).

    Legacy witnessed runs are a saved-proof LOWER BOUND, not a complete event
    count: old producers collapsed by beneficiary and truncated at two.
    """
    review = review if isinstance(review, dict) else {}
    if 'carrying_frequency' in review:
        summary = validate_frequency_summary(review['carrying_frequency'])
        if summary is not None:
            # Full/light publication callers supply these saved references.
            # A self-consistent summary must not invent events or a different
            # sample beyond its own frozen witnesses and selected run IDs.
            if 'witnesses' in review:
                witnesses = review['witnesses']
                ids = witnesses.get('carrying') if isinstance(witnesses, dict) else None
                if not _ids(ids) or not set(summary['verified_run_ids']) <= set(ids):
                    summary = None
            if summary is not None and 'structure' in review:
                structure = review['structure']
                ids = structure.get('recent_run_ids') if isinstance(structure, dict) else None
                if not _ids(ids) or set(summary['observed_run_ids']) != set(ids):
                    summary = None
        if summary is not None:
            return summary
        return dict(supported=False, legacy=False, invalid=True, verified_runs=None,
                    observed_runs=None, unknown_runs=None, distinct_beneficiaries=None,
                    verified_run_ids=[])
    structure = review.get('structure') or {}
    resolution = (review.get('label_reviews') or {}).get('carrying') or {}
    saved = list((review.get('witnesses') or {}).get('carrying') or [])
    saved += resolution.get('supported_run_ids') or []
    ids = sorted({rid for rid in saved if type(rid) is int and rid > 0})
    observed_ids = structure.get('recent_run_ids')
    observed = len(set(observed_ids)) if _ids(observed_ids) else structure.get('recent_runs')
    if type(observed) is not int or observed < len(ids) or observed > SAMPLE_LIMIT:
        observed = None
    return dict(supported=False, legacy=True, saved_proof_lower_bound=True,
        window_days=WINDOW_DAYS, sample_limit=SAMPLE_LIMIT, min_verified_runs=MIN_VERIFIED_RUNS,
        min_distinct_beneficiaries=MIN_DISTINCT_BENEFICIARIES, verified_runs=len(ids),
        observed_runs=observed, unknown_runs=None, distinct_beneficiaries=None,
        verified_run_ids=ids)


def stable_carrying_frequency(review):
    """Convenience predicate returning validated stable metadata or None."""
    summary = assess_carrying_frequency(review)
    return summary if summary['supported'] else None
