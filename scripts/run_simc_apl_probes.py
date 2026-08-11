#!/usr/bin/env python3
"""按官方专精隔离运行 patched SimC action/Buff 元数据探针。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from export_simc_apl_fields import official_specs


PROFILE_CLASS_KEYS = {
    "deathknight": "death_knight",
    "demonhunter": "demon_hunter",
    "druid": "druid",
    "evoker": "evoker",
    "hunter": "hunter",
    "mage": "mage",
    "monk": "monk",
    "paladin": "paladin",
    "priest": "priest",
    "rogue": "rogue",
    "shaman": "shaman",
    "warlock": "warlock",
    "warrior": "warrior",
}

INLINE_ACTOR_KEYS = {
    "death_knight": "deathknight",
    "demon_hunter": "demonhunter",
    "druid": "druid",
    "evoker": "evoker",
    "hunter": "hunter",
    "mage": "mage",
    "monk": "monk",
    "paladin": "paladin",
    "priest": "priest",
    "rogue": "rogue",
    "shaman": "shaman",
    "warlock": "warlock",
    "warrior": "warrior",
}

ACTOR_RE = re.compile(
    r"(?m)^(?P<class>deathknight|demonhunter|druid|evoker|hunter|mage|monk|"
    r"paladin|priest|rogue|shaman|warlock|warrior)\s*="
)
SPEC_RE = re.compile(r"(?m)^spec\s*=\s*(?P<spec>[a-z_]+)\s*$")


@dataclass(frozen=True)
class ProbeJob:
    class_name: str
    spec: str
    actor_args: tuple[str, ...]
    output_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="隔离运行全部官方 SimC APL 专精探针")
    parser.add_argument("--simc-source", type=Path, required=True)
    parser.add_argument("--simc-binary", type=Path, required=True)
    parser.add_argument("--probe-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--game-build", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--index-output", type=Path, required=True)
    return parser.parse_args()


def profile_inventory(simc_source: Path) -> dict[tuple[str, str], list[Path]]:
    inventory: dict[tuple[str, str], list[Path]] = {}
    profile_dir = simc_source / "profiles" / "MID1"
    for path in sorted(profile_dir.glob("*.simc"), key=lambda value: (len(value.name), value.name)):
        text = path.read_text(encoding="utf-8", errors="replace")
        actor = ACTOR_RE.search(text)
        spec = SPEC_RE.search(text)
        if not actor or not spec:
            continue
        class_name = PROFILE_CLASS_KEYS[actor.group("class")]
        key = (class_name, spec.group("spec"))
        inventory.setdefault(key, []).append(path.resolve())
    return inventory


def build_jobs(simc_source: Path, output_dir: Path) -> list[ProbeJob]:
    supported = official_specs(simc_source)
    profiles = profile_inventory(simc_source)
    jobs = []
    for class_name, specs in sorted(supported.items()):
        for spec in sorted(specs):
            available = profiles.get((class_name, spec), [])
            if available:
                actor_args = (str(available[0]),)
            else:
                actor_key = INLINE_ACTOR_KEYS[class_name]
                actor_args = (
                    f"{actor_key}=Runtime_{class_name}_{spec}",
                    f"spec={spec}",
                    "default_actions=1",
                )
                if spec == "restoration":
                    actor_args += ("role=heal",)
            jobs.append(ProbeJob(
                class_name=class_name,
                spec=spec,
                actor_args=actor_args,
                output_path=(output_dir / f"{class_name}-{spec}.json").resolve(),
            ))
    return jobs


def run_job(
    job: ProbeJob,
    *,
    binary: Path,
    simc_source: Path,
    probe_file: Path,
    revision: str,
    game_build: str,
    timeout: int,
) -> dict:
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(binary),
        *job.actor_args,
        f"apl_metadata_probe_file={probe_file}",
        f"apl_metadata_export={job.output_path}",
        f"apl_metadata_revision={revision}",
        f"apl_metadata_game_build={game_build}",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=simc_source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(10, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "class": job.class_name,
            "spec": job.spec,
            "status": "timeout",
            "returncode": None,
            "manifest": "",
            "diagnostic": str(exc),
        }
    if result.returncode != 0 or not job.output_path.exists():
        diagnostic = "\n".join((result.stdout, result.stderr)).strip()
        return {
            "class": job.class_name,
            "spec": job.spec,
            "status": "failed",
            "returncode": result.returncode,
            "manifest": "",
            "diagnostic": diagnostic[-3000:],
        }
    payload = json.loads(job.output_path.read_text(encoding="utf-8"))
    trusted = [
        item for item in payload.get("symbols", [])
        if item.get("kind") == "action"
        and item.get("source") in {"runtime_apl_action", "runtime_action_probe"}
    ]
    return {
        "class": job.class_name,
        "spec": job.spec,
        "status": "ok",
        "returncode": result.returncode,
        "manifest": str(job.output_path),
        "symbol_count": len(payload.get("symbols", [])),
        "trusted_actions": len(trusted),
        "bound_trusted_actions": sum(bool(item.get("spell_id")) for item in trusted),
        "diagnostic": "",
    }


def load_probe_tokens(path: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        class_name, separator, token = line.partition("\t")
        if separator and class_name and token:
            result.setdefault(class_name, []).append(token)
    return {key: sorted(set(values)) for key, values in result.items()}


def write_subset_probe(path: Path, class_name: str, tokens: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{class_name}\t{token}\n" for token in tokens),
        encoding="utf-8",
    )


def run_job_with_split(
    job: ProbeJob,
    *,
    tokens: list[str],
    binary: Path,
    simc_source: Path,
    probe_root: Path,
    revision: str,
    game_build: str,
    timeout: int,
) -> dict:
    """二分隔离会导致 SimC 进程崩溃的单个候选，保留其余安全结果。"""

    attempts = 0

    def execute(subset: list[str], label: str) -> tuple[list[dict], list[str]]:
        nonlocal attempts
        attempts += 1
        subset_probe = probe_root / "probe-subsets" / f"{job.class_name}-{job.spec}-{label}.tsv"
        subset_manifest = probe_root / f"{job.class_name}-{job.spec}-{label}.json"
        write_subset_probe(subset_probe, job.class_name, subset)
        attempt_job = ProbeJob(job.class_name, job.spec, job.actor_args, subset_manifest)
        result = run_job(
            attempt_job,
            binary=binary,
            simc_source=simc_source,
            probe_file=subset_probe,
            revision=revision,
            game_build=game_build,
            timeout=timeout,
        )
        if result["status"] == "ok":
            return [result], []
        if len(subset) <= 1:
            return [], subset
        middle = len(subset) // 2
        left_results, left_unsafe = execute(subset[:middle], f"{label}0")
        right_results, right_unsafe = execute(subset[middle:], f"{label}1")
        return left_results + right_results, left_unsafe + right_unsafe

    successful, unsafe_tokens = execute(tokens, "all")
    manifests = [item["manifest"] for item in successful]
    return {
        "class": job.class_name,
        "spec": job.spec,
        "status": "ok_with_unsafe" if manifests and unsafe_tokens else ("ok" if manifests else "failed"),
        "returncode": 0 if manifests else None,
        "manifest": manifests[0] if len(manifests) == 1 else "",
        "manifests": manifests,
        "symbol_count": sum(item.get("symbol_count", 0) for item in successful),
        "trusted_actions": sum(item.get("trusted_actions", 0) for item in successful),
        "bound_trusted_actions": sum(item.get("bound_trusted_actions", 0) for item in successful),
        "unsafe_tokens": unsafe_tokens,
        "attempts": attempts,
        "diagnostic": "" if manifests else "所有候选子集均失败",
    }


def main() -> int:
    args = parse_args()
    simc_source = args.simc_source.resolve()
    binary = args.simc_binary.resolve()
    probe_file = args.probe_file.resolve()
    output_dir = args.output_dir.resolve()
    jobs = build_jobs(simc_source, output_dir)
    probe_tokens = load_probe_tokens(probe_file)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                run_job_with_split,
                job,
                tokens=probe_tokens.get(job.class_name, []),
                binary=binary,
                simc_source=simc_source,
                probe_root=output_dir,
                revision=args.revision,
                game_build=args.game_build,
                timeout=args.timeout,
            ): job
            for job in jobs
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            print(
                f"[{index}/{len(jobs)}] {result['class']}/{result['spec']}: "
                f"{result['status']} actions={result.get('trusted_actions', 0)} "
                f"bound={result.get('bound_trusted_actions', 0)} "
                f"unsafe={len(result.get('unsafe_tokens', []))}",
                flush=True,
            )
    results.sort(key=lambda item: (item["class"], item["spec"]))
    index_payload = {
        "schema_version": 1,
        "simc_revision": args.revision,
        "game_build": args.game_build,
        "probe_file": str(probe_file),
        "results": results,
    }
    args.index_output.parent.mkdir(parents=True, exist_ok=True)
    args.index_output.write_text(
        json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    failures = [result for result in results if result["status"] == "failed"]
    unsafe = sorted({
        (result["class"], token)
        for result in results
        for token in result.get("unsafe_tokens", [])
    })
    print(
        f"探针完成: total={len(results)} ok={len(results) - len(failures)} "
        f"failed={len(failures)} unsafe_tokens={len(unsafe)}"
    )
    if failures:
        for result in failures:
            print(f"失败: {result['class']}/{result['spec']} {result['diagnostic'][:500]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
