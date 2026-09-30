"""Strict local native on/off probe; no database writes or production calls."""

import argparse, copy, hashlib, json, math, os, pathlib, subprocess, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if not __debug__:
    raise SystemExit("Do not disable assertions in this evidence verifier.")
AMOUNTS = {"hit", "crit", "expected", "noncrit_contribution", "crit_contribution"}
COMPONENT_AMOUNTS = AMOUNTS | {"target_" + v for v in AMOUNTS}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


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


def export(source, tag, excluded=()):
    assert digest(BINARY) == BINARY_SHA and digest(source) == INPUT_SHA, (
        "Binary/input changed during probe"
    )
    path = R / (tag + ".json")
    log = R / (tag + ".log")
    env = dict(os.environ)
    env.pop("LMONITOR_SIMC_PARSER_EXCLUDE", None)
    if excluded:
        env["LMONITOR_SIMC_PARSER_EXCLUDE"] = ",".join(
            f"{s}:{i}" for s, i in sorted(excluded)
        )
    command = [
        str(BINARY),
        str(source),
        "threads=1",
        f"skill_damage_target_health_percentage={HEALTH}",
        f"skill_damage_export={path}",
        f"skill_damage_revision={REVISION}",
        f"skill_damage_game_build={GAME_BUILD}",
    ]
    start = time.monotonic()
    with log.open("w") as stream:
        done = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
    assert digest(BINARY) == BINARY_SHA and digest(source) == INPUT_SHA, (
        "Binary/input changed during probe"
    )
    assert done.returncode == 0, (tag, done.returncode, str(log))
    assert "Unknown option" not in log.read_text()
    return json.loads(path.read_text()), {
        "file": str(path),
        "sha256": digest(path),
        "input_sha256": digest(source),
        "elapsed_seconds": time.monotonic() - start,
        "excluded": [list(x) for x in sorted(excluded)],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=pathlib.Path, required=True)
    parser.add_argument("--input", type=pathlib.Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--game-build", required=True)
    parser.add_argument("--target-health", type=float, default=100)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--max-effects", type=int, default=16)
    args = parser.parse_args()
    BINARY = args.binary.resolve()
    source = args.input.resolve()
    R = args.output_dir.resolve()
    REVISION = args.revision
    GAME_BUILD = args.game_build
    HEALTH = args.target_health
    assert BINARY.is_file() and source.is_file()
    assert len(REVISION) == 40 and all(
        c in "0123456789abcdefABCDEF" for c in REVISION
    ), "Expected exact upstream SHA"
    BINARY_SHA = digest(BINARY)
    INPUT_SHA = digest(source)
    assert 0 < HEALTH <= 100 and args.max_effects > 0
    R.mkdir(parents=True, exist_ok=False)
    tag = "probe"
    baseline, base_record = export(source, tag + "-on")
    candidates = set()
    for actor in baseline["actors"]:
        for action in actor["actions"]:
            for amount in [
                action["baseline"],
                *[s["values"] for s in action.get("scenarios", [])],
            ]:
                for kind in ("direct", "tick"):
                    comp = amount.get(kind) if isinstance(amount, dict) else None
                    if isinstance(comp, dict):
                        candidates.update(
                            effect_key(e)
                            for e in comp["runtime_layers"][
                                "specialization_passive_effects"
                            ]
                        )
    assert len(candidates) <= args.max_effects, (
        "Candidate limit exceeded; no counterfactuals launched"
    )
    report = {
        "input": str(source),
        "scope": "explicit frozen input only; not a published snapshot",
        "binary_sha256": digest(BINARY),
        "baseline": base_record,
        "candidates": [list(x) for x in sorted(candidates)],
        "probes": [],
    }
    groups = [{c} for c in sorted(candidates)] + (
        [candidates] if len(candidates) > 1 else []
    )
    for excluded in groups:
        suffix = "-".join(f"{s}_{i}" for s, i in sorted(excluded))
        payload, record = export(source, tag + "-off-" + suffix, excluded)
        try:
            record.update(compare(baseline, payload, excluded))
            record["status"] = "compared"
        except Exception as exc:
            record.update(status="rejected", reason=repr(exc))
        report["probes"].append(record)
        (R / (tag + "-report.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    k: v
                    for k, v in record.items()
                    if k not in ("verified", "rejected", "unregistered")
                }
                | {
                    k: len(record.get(k, []))
                    for k in ("verified", "rejected", "unregistered")
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    (R / (tag + "-report.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    assert all(p["status"] == "compared" for p in report["probes"]), (
        "See per-probe report; do not authorize failed comparisons"
    )
