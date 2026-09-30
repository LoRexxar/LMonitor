"""Shared native parser-only counterfactual checks; no I/O or DB writes.

Serialized applied flags are not evidence. Only actual paired outputs from the
same frozen input and binary may be compared at this boundary.
"""

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass

AMOUNTS = {"hit", "crit", "expected", "noncrit_contribution", "crit_contribution"}
COMPONENT_AMOUNTS = AMOUNTS | {"target_" + v for v in AMOUNTS}


def same(a, b):
    if type(a) in (int, float) and type(b) in (int, float):
        return (
            math.isfinite(a)
            and math.isfinite(b)
            and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8)
        )
    if type(a) != type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def effect_key(e):
    return e["source_spell_id"], e["effect_index"]


def action_key(a):
    return a["token"], a["spell_id"]


def ledger(a):
    result = {}
    for row in a["passive_parser_ledger"]:
        key = row["action_spell_id"], row["component"]
        assert key not in result
        effects = {effect_key(e): e for e in row["effects"]}
        assert len(effects) == len(row["effects"])
        result[key] = {"flat": row["flat"], "pct": row["pct"], "effects": effects}
    return result


def compare_ledgers(on, off, excluded):
    left, right = ledger(on), ledger(off)
    factors = {}
    for key in left.keys() | right.keys():
        a = left.get(key, {"flat": 0, "pct": 1, "effects": {}})
        b = right.get(key, {"flat": 0, "pct": 1, "effects": {}})
        removed = {k: v for k, v in a["effects"].items() if k in excluded}
        assert b["effects"].keys() == a["effects"].keys() - removed.keys(), (
            "unexpected_parser_identity_change",
            key,
        )
        assert all(
            same(v, b["effects"][k])
            for k, v in a["effects"].items()
            if k not in removed
        ), ("other_parser_value_changed", key)
        assert same(a["flat"], b["flat"]), ("parser_flat_changed", key)
        factor = 1.0
        for v in removed.values():
            assert v["subtype"] in (108, 218), ("not_a_percent_modifier", v)
            f = 1 + v["value"] / 100
            assert math.isfinite(f) and f > 0
            factor *= f
        assert same(a["pct"], b["pct"] * factor), ("parser_product_does_not_match", key)
        factors[key] = (factor, removed)
    return factors


def stripped_effects(effects):
    return [{k: v for k, v in e.items() if k != "parser_consistency"} for e in effects]


def compare_component(on, off, kind, factor):
    assert isinstance(on, dict) and isinstance(off, dict), "component_missing"
    # Check all other component facts, not only a hand-picked visible multiplier.
    a = copy.deepcopy(on)
    b = copy.deepcopy(off)
    for field in COMPONENT_AMOUNTS:
        if field not in a and field not in b:
            continue
        assert field in a and field in b, ("amount_field_missing", field)
        x, y = a.pop(field), b.pop(field)
        if isinstance(x, dict):
            assert isinstance(y, dict) and x.keys() == y.keys(), (
                "target_map_changed",
                field,
            )
            assert all(same(x[k], y[k] * factor) for k in x), (
                "target_amount_ratio",
                field,
            )
        else:
            assert same(x, y * factor), ("amount_ratio", field)
    assert on["hit"] > 0 and off["hit"] > 0, "zero_damage_is_not_positive_proof"
    aggregate = "da_multiplier" if kind == "direct" else "ta_multiplier"
    la, lb = a["runtime_layers"], b["runtime_layers"]
    assert same(la.pop(aggregate), lb.pop(aggregate) * factor), "state_multiplier_ratio"
    la["specialization_passive_effects"] = stripped_effects(
        la["specialization_passive_effects"]
    )
    lb["specialization_passive_effects"] = stripped_effects(
        lb["specialization_passive_effects"]
    )
    aa = a["base_damage_layers"]
    bb = b["base_damage_layers"]
    assert same(
        aa.pop("component_multiplier"), bb.pop("component_multiplier") * factor
    ), "action_base_ratio"
    assert same(a, b), "other_component_facts_changed"
    return {
        "factor": factor,
        "on_hit": on["hit"],
        "off_hit": off["hit"],
        "on_layer": on["runtime_layers"][aggregate],
        "off_layer": off["runtime_layers"][aggregate],
    }


def compare(on, off, excluded):
    if not __debug__:
        raise ValueError("Native evidence validation requires enabled assertions")
    expected = {
        "method": "initialization_parser_only_v1",
        "excluded_effects": [
            {"source_spell_id": s, "effect_index": i} for s, i in sorted(excluded)
        ],
    }
    assert (
        off.get("parser_counterfactual") == expected
        and "parser_counterfactual" not in on
    )
    assert same(
        {k: v for k, v in on.items() if k != "actors"},
        {k: v for k, v in off.items() if k not in ("actors", "parser_counterfactual")},
    ), "export_identity_changed"
    left = {a["name"]: a for a in on["actors"]}
    right = {a["name"]: a for a in off["actors"]}
    assert left.keys() == right.keys()
    assert (
        left and len(left) == len(on["actors"]) and len(right) == len(off["actors"])
    ), "ambiguous_actor_identity"
    result = {"verified": [], "rejected": [], "unregistered": []}
    for name, a in left.items():
        b = right[name]
        assert same(
            {
                k: v
                for k, v in a.items()
                if k not in ("actions", "passive_parser_ledger")
            },
            {
                k: v
                for k, v in b.items()
                if k not in ("actions", "passive_parser_ledger")
            },
        ), "actor_metadata_changed"
        factors = compare_ledgers(a, b, excluded)
        actions = {action_key(x): x for x in a["actions"]}
        others = {action_key(x): x for x in b["actions"]}
        assert actions.keys() == others.keys(), "action_identity_set_changed"
        assert len(actions) == len(a["actions"]) and len(others) == len(b["actions"]), (
            "ambiguous_action_identity"
        )
        for key, x in actions.items():
            y = others[key]
            if not same(
                {k: v for k, v in x.items() if k not in ("baseline", "scenarios")},
                {k: v for k, v in y.items() if k not in ("baseline", "scenarios")},
            ):
                result["rejected"].append(
                    {"actor": name, "action": key, "reason": "action_metadata_changed"}
                )
                continue

            def amounts(action):
                entries = {"baseline": (action["baseline"], {})}
                for scenario in action.get("scenarios") or []:
                    metadata = {k: v for k, v in scenario.items() if k != "values"}
                    # Scenario identity is its actual conditions, never rounded delta outputs.
                    skey = json.dumps(
                        sorted(
                            scenario["buffs"],
                            key=lambda b: json.dumps(b, sort_keys=True),
                        ),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    assert skey not in entries
                    entries[skey] = (scenario["values"], metadata)
                return entries

            am, bm = amounts(x), amounts(y)
            for scenario, (v, metadata) in am.items():
                pair = bm.get(scenario)
                w = pair[0] if pair is not None else None
                for kind in ("direct", "tick"):
                    component = v.get(kind) if isinstance(v, dict) else None
                    if not isinstance(component, dict):
                        continue
                    native_candidates = component["runtime_layers"][
                        "specialization_passive_effects"
                    ]
                    assert len({effect_key(e) for e in native_candidates}) == len(
                        native_candidates
                    ), "duplicate_candidate_identity"
                    candidates = {
                        effect_key(e): e
                        for e in native_candidates
                        if effect_key(e) in excluded
                    }
                    if not candidates:
                        continue
                    factor, removed = factors.get((x["spell_id"], kind), (1, {}))
                    item = {
                        "actor": name,
                        "action": key,
                        "component": kind,
                        "scenario": scenario,
                        "effects": [list(k) for k in candidates],
                    }
                    if not removed:
                        # A DBC-only candidate such as a deregistered weapon passive is a negative control.
                        item["reason"] = "not_registered"
                        result["unregistered"].append(item)
                        assert w is not None and same(component, w.get(kind)), (
                            "unregistered_candidate_changed",
                            key,
                        )
                        continue
                    try:
                        assert set(removed) <= set(candidates), (
                            "parser_effect_not_in_candidate_scope"
                        )
                        assert len(removed) != 1 or not math.isclose(
                            factor, 1.0, rel_tol=1e-8
                        ), "no_damage_marginal"
                        assert pair is not None and w is not None, "scenario_missing"
                        assert same(metadata, pair[1]), "scenario_metadata_changed"
                        assert (
                            v.get("unresolved_reason")
                            == w.get("unresolved_reason")
                            == None
                        ), "unresolved_component"
                        item.update(
                            compare_component(component, w.get(kind), kind, factor)
                        )
                        item["effects"] = [list(k) for k in removed]
                        result["verified"].append(item)
                    except (AssertionError, TypeError, KeyError) as exc:
                        item["reason"] = str(exc)
                        result["rejected"].append(item)
    return result


@dataclass(frozen=True)
class PassiveProbeExport:
    """Execution envelope from the trusted runner, never a client applied flag.

    The runner must pin/hash the input and binary around each actual execution.
    This class does not authenticate caller-supplied JSON or launch a process.
    """

    payload: dict
    input_sha256: str
    binary_sha256: str


def payload_signature(payload):
    """Canonical streaming digest without a second full serialized payload."""
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(payload):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _record_key(record):
    return (
        record["actor"],
        tuple(record["action"]),
        record["component"],
        record["scenario"],
    )


def _probe_identity(probe):
    if not isinstance(probe, PassiveProbeExport) or not isinstance(probe.payload, dict):
        raise ValueError("Missing native execution identity envelope")
    values = (probe.input_sha256, probe.binary_sha256)
    if any(
        not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in values
    ):
        raise ValueError("Invalid native execution identity")
    return values


def required_exact_joint_groups(probe_records):
    """Plan only multi-source sets observed in verified singleton results."""
    by_component = {}
    for probe in probe_records:
        if probe.get("status") != "compared" or len(probe.get("excluded", [])) != 1:
            continue
        effect = tuple(probe["excluded"][0])
        for record in probe.get("verified", []):
            if {tuple(e) for e in record["effects"]} != {effect}:
                raise ValueError("Single probe source identity mismatch")
            by_component.setdefault(_record_key(record), set()).add(effect)
    groups = {
        frozenset(effects) for effects in by_component.values() if len(effects) > 1
    }
    return sorted(groups, key=lambda g: (len(g), sorted(g)))


def verify_passive_applications(ordinary, counterfactuals):
    """Compile independent + joint component/state evidence, without mutation.

    Incomplete execution sets are errors. A compared component that does not
    pass the numerical/semantic gates is diagnosed, never authorized.
    """
    identity = _probe_identity(ordinary)
    on = ordinary.payload
    if "parser_counterfactual" in on:
        raise ValueError("Expected ordinary native export, not diagnostic input")
    candidates = set()
    for actor in on.get("actors") or []:
        for action in actor.get("actions") or []:
            amounts = [action.get("baseline")] + [
                s.get("values") for s in action.get("scenarios") or []
            ]
            for amount in amounts:
                for kind in ("direct", "tick"):
                    component = amount.get(kind) if isinstance(amount, dict) else None
                    if isinstance(component, dict):
                        candidates.update(
                            effect_key(e)
                            for e in component["runtime_layers"][
                                "specialization_passive_effects"
                            ]
                        )
    comparisons = {}
    diagnostics = []
    hashes = []
    for probe in counterfactuals:
        if _probe_identity(probe) != identity:
            raise ValueError("Counterfactual execution identity mismatch")
        metadata = probe.payload.get("parser_counterfactual")
        try:
            excluded = frozenset(effect_key(e) for e in metadata["excluded_effects"])
        except (TypeError, KeyError) as exc:
            raise ValueError("Missing counterfactual identity") from exc
        if not excluded:
            raise ValueError("Empty counterfactual exclusion")
        if excluded in comparisons:
            raise ValueError("duplicate counterfactual exclusion")
        try:
            result = compare(on, probe.payload, excluded)
        except (AssertionError, TypeError, KeyError, OverflowError) as exc:
            raise ValueError("Invalid counterfactual evidence: " + str(exc)) from exc
        comparisons[excluded] = {_record_key(v): v for v in result["verified"]}
        diagnostics.extend(
            {**v, "excluded_effects": [list(e) for e in sorted(excluded)]}
            for v in result["rejected"] + result["unregistered"]
        )
        hashes.append(
            {
                "excluded_effects": [list(e) for e in sorted(excluded)],
                "payload_sha256": payload_signature(probe.payload),
            }
        )
    if any(frozenset([e]) not in comparisons for e in candidates):
        raise ValueError("Missing single-effect counterfactual")
    # A global union can change other sources and mask a smaller subset's
    # interactions. Authorize only the exact independently proven source set
    # for this component/state. A singleton is already its own joint probe.
    by_component = {}
    for effect in sorted(candidates):
        for key, record in comparisons[frozenset([effect])].items():
            by_component.setdefault(key, {})[effect] = record
    authorized = []
    for key, single_records in sorted(by_component.items()):
        effects = sorted(single_records)
        group = frozenset(effects)
        record = comparisons.get(group, {}).get(key)
        if record is None:
            diagnostics.append(
                {
                    **next(iter(single_records.values())),
                    "effects": [list(e) for e in effects],
                    "reason": (
                        "missing_exact_joint_application"
                        if group not in comparisons
                        else "joint_application_not_proven"
                    ),
                }
            )
            continue
        if {tuple(e) for e in record["effects"]} != group:
            diagnostics.append({**record, "reason": "joint_application_scope_mismatch"})
            continue
        factors = [single_records[e]["factor"] for e in effects]
        if not math.isclose(
            math.prod(factors), record["factor"], rel_tol=1e-8, abs_tol=1e-8
        ):
            diagnostics.append({**record, "reason": "joint_application_not_separable"})
            continue
        authorized.append(
            {
                **record,
                "action": list(record["action"]),
                "effects": [list(e) for e in effects],
                "source_factors": [
                    {"source_spell_id": e[0], "effect_index": e[1], "factor": f}
                    for e, f in zip(effects, factors)
                ],
            }
        )
    return {
        "method": "initialization_parser_only_exact_joint_v1",
        "input_sha256": identity[0],
        "binary_sha256": identity[1],
        "source_payload_sha256": payload_signature(on),
        "counterfactuals": hashes,
        "components": authorized,
        "diagnostics": diagnostics,
    }
