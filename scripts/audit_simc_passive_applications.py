"""Strict local native on/off probe; no database writes or production calls."""

import argparse, hashlib, json, os, pathlib, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if not __debug__:
    raise SystemExit("Do not disable assertions in this evidence verifier.")
# The CLI and backend must use the same evidence predicate.
sys.path.insert(0, str(ROOT))
from botend.services.simc_skill_passive_evidence import (
    compare,
    effect_key,
    required_exact_joint_groups,
)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


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
        "binary_sha256": BINARY_SHA,
        "target_health_percentage": HEALTH,
        "command": command,
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
    parser.add_argument("--max-joint-groups", type=int, default=16)
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
    assert 0 < HEALTH <= 100 and args.max_effects > 0 and args.max_joint_groups >= 0
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
        "target_health_percentage": HEALTH,
        "baseline": base_record,
        "candidates": [list(x) for x in sorted(candidates)],
        "probes": [],
    }
    groups = [{c} for c in sorted(candidates)]
    for excluded in groups:
        suffix = "-".join(f"{s}_{i}" for s, i in sorted(excluded))
        payload, record = export(source, tag + "-off-" + suffix, excluded)
        try:
            record.update(compare(baseline, payload, excluded))
            record["status"] = "compared"
        except Exception as exc:
            record.update(status="rejected", reason=repr(exc))
        report["probes"].append(record)
        if len(report["probes"]) == len(candidates):
            exact_groups = required_exact_joint_groups(report["probes"])
            assert len(exact_groups) <= args.max_joint_groups, (
                "Exact joint limit exceeded"
            )
            groups.extend(exact_groups)
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
