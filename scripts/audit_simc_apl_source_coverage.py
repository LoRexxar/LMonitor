#!/usr/bin/env python3
"""审计锁定 SimC 源码的静态技能与状态字段是否全部进入元数据包。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from export_simc_apl_fields import (
    ACTION_FUNCTION_RE,
    CLASS_OWNER_KEYS,
    STRING_VIEW_PARAMETER_RE,
    comparison_tokens,
    extract_static_action_candidates,
    matching_brace,
)


STATE_CALL_RE = re.compile(
    r"\b(?:make_buff|make_fallback|make_debuff|MBF)(?:\s*<[^;{}()]*>)?\s*\("
)
STRING_LITERAL_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
CLASS_PATH_MARKERS = tuple(
    sorted(
        ((class_name, owner.removesuffix("_t")) for owner, class_name in CLASS_OWNER_KEYS.items()),
        key=lambda item: len(item[1]),
        reverse=True,
    )
)


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON 根节点必须是对象：{path}")
    return payload


def _class_from_path(path: Path) -> str | None:
    normalized = path.as_posix().lower()
    for class_name, marker in CLASS_PATH_MARKERS:
        if f"/{marker}/" in normalized or f"sc_{marker}." in normalized or f"apl_{marker}." in normalized:
            return class_name
    return None


def _matching_parenthesis(text: str, open_index: int) -> int:
    depth = 0
    state = "code"
    index = open_index
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                state = "line_comment"
                index += 2
                continue
            if char == "/" and next_char == "*":
                state = "block_comment"
                index += 2
                continue
            if char == '"':
                state = "string"
            elif char == "'":
                state = "char"
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
        elif state == "line_comment":
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            if char == "*" and next_char == "/":
                state = "code"
                index += 2
                continue
        elif state in {"string", "char"}:
            quote = '"' if state == "string" else "'"
            if char == "\\":
                index += 2
                continue
            if char == quote:
                state = "code"
        index += 1
    raise ValueError(f"找不到从偏移 {open_index} 开始的调用结束位置")


def extract_static_state_candidates(simc_source: Path) -> list[dict]:
    """提取职业模块状态构造调用中的首个字符串字面量，包括动态名称前缀。"""
    root = simc_source / "engine" / "class_modules"
    candidates: dict[tuple[str, str], dict] = {}
    for path in sorted((*root.rglob("*.cpp"), *root.rglob("*.hpp"))):
        class_name = _class_from_path(path)
        if not class_name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in STATE_CALL_RE.finditer(text):
            open_index = text.find("(", match.start(), match.end())
            if open_index < 0:
                continue
            end_index = _matching_parenthesis(text, open_index)
            literal = STRING_LITERAL_RE.search(text, open_index + 1, end_index)
            if not literal:
                continue
            token = bytes(literal.group(1), "utf-8").decode("unicode_escape").strip().lower()
            if not token or not re.fullmatch(r"[a-z0-9_]+", token):
                continue
            key = (class_name, token)
            candidates.setdefault(key, {
                "class": class_name,
                "token": token,
                "source_file": path.relative_to(simc_source).as_posix(),
                "source_line": text.count("\n", 0, match.start()) + 1,
            })
    return [candidates[key] for key in sorted(candidates)]


def extract_dynamic_state_calls(simc_source: Path) -> list[dict]:
    """列出没有字符串字面量、必须通过构造器或运行时名称解析的状态调用。"""
    root = simc_source / "engine" / "class_modules"
    calls = []
    for path in sorted((*root.rglob("*.cpp"), *root.rglob("*.hpp"))):
        class_name = _class_from_path(path)
        if not class_name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in STATE_CALL_RE.finditer(text):
            open_index = text.find("(", match.start(), match.end())
            if open_index < 0:
                continue
            end_index = _matching_parenthesis(text, open_index)
            if STRING_LITERAL_RE.search(text, open_index + 1, end_index):
                continue
            calls.append({
                "class": class_name,
                "source_file": path.relative_to(simc_source).as_posix(),
                "source_line": text.count("\n", 0, match.start()) + 1,
            })
    return sorted(calls, key=lambda row: (row["source_file"], row["source_line"]))


def extract_unclassified_action_literals(simc_source: Path, covered: set[tuple[str, str]]) -> list[dict]:
    """找出职业动作工厂内不是已知动作 token 的小写字面量，防止表驱动工厂漏扫。"""
    rows = []
    engine = simc_source / "engine"
    for path in sorted((*engine.rglob("*.cpp"), *engine.rglob("*.hpp"))):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in ACTION_FUNCTION_RE.finditer(text):
            owner = match.group("owner").rstrip(":").split("::")[-1]
            class_name = CLASS_OWNER_KEYS.get(owner)
            parameters = list(STRING_VIEW_PARAMETER_RE.finditer(match.group("params")))
            if not class_name or not parameters:
                continue
            parameter = parameters[0].group("name")
            end_index = matching_brace(text, match.end() - 1)
            body = text[match.end():end_index]
            direct_tokens = comparison_tokens(body, parameter)
            literals = set(re.findall(r'"([a-z0-9_]+)"', body))
            for literal in sorted(literals - direct_tokens):
                if (class_name, literal) in covered:
                    continue
                rows.append({
                    "class": class_name,
                    "literal": literal,
                    "source_file": path.relative_to(simc_source).as_posix(),
                    "source_line": text.count("\n", 0, match.start()) + 1,
                })
    return rows


def _source_revision(simc_source: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=simc_source,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().lower()


def audit_source_coverage(
    simc_source: Path,
    source_payload: dict,
    supplement_payload: dict,
    package_payload: dict | None = None,
) -> dict:
    revision = str(source_payload.get("simc_revision") or "").lower()
    build = str(source_payload.get("game_build") or "")
    failures: list[str] = []
    if _source_revision(simc_source) != revision:
        failures.append("SimC 源码 HEAD 与字段清单 revision 不一致")
    for field, expected in (("simc_revision", revision), ("game_build", build)):
        if str(supplement_payload.get(field) or "").lower() != expected.lower():
            failures.append(f"源码补充包的 {field} 与字段清单不一致")
        if package_payload is not None and str(package_payload.get(field) or "").lower() != expected.lower():
            failures.append(f"最终元数据包的 {field} 与字段清单不一致")

    source_records = source_payload.get("records") or []
    supplements = supplement_payload.get("records") or []
    runtime_actions = {
        (str(row.get("class") or "").lower(), str(row.get("token") or "").lower())
        for row in source_records if row.get("kind") == "action"
    }
    runtime_states = {
        (str(row.get("class") or "").lower(), str(row.get("token") or "").lower())
        for row in source_records if row.get("kind") in {"buff", "debuff"}
    }
    supplement_actions = {
        (str(row.get("class") or "").lower(), str(row.get("token") or "").lower())
        for row in supplements if row.get("kind") == "action"
    }
    supplement_states = {
        (
            str(row.get("class") or "").lower(),
            str(row.get("dynamic_source_token") or row.get("token") or "").lower(),
        )
        for row in supplements if row.get("kind") in {"buff", "debuff"}
    }
    supplement_state_tokens = {
        (str(row.get("class") or "").lower(), str(row.get("token") or "").lower())
        for row in supplements if row.get("kind") in {"buff", "debuff"}
    }

    static_actions = {
        (item.class_name, item.token) for item in extract_static_action_candidates(simc_source)
    }
    static_state_rows = extract_static_state_candidates(simc_source)
    static_states = {(row["class"], row["token"]) for row in static_state_rows}
    missing_actions = sorted(static_actions - runtime_actions - supplement_actions)
    missing_states = sorted(static_states - runtime_states - supplement_states)
    if missing_actions:
        failures.append(f"仍有 {len(missing_actions)} 个静态技能动作未覆盖")
    if missing_states:
        failures.append(f"仍有 {len(missing_states)} 个静态 Buff/Debuff 未覆盖")

    extra_action_literals = extract_unclassified_action_literals(
        simc_source, runtime_actions | supplement_actions,
    )
    extra_action_keys = {
        (row["class"], row["source_file"], row["source_line"], row["literal"])
        for row in extra_action_literals
    }
    declared_action_literals = set()
    for declaration in supplement_payload.get("action_factory_non_token_literals") or []:
        for literal in declaration.get("literals") or []:
            declared_action_literals.add((
                str(declaration.get("class") or "").lower(),
                str(declaration.get("source_file") or ""),
                int(declaration.get("source_line") or 0),
                str(literal or "").lower(),
            ))
    missing_action_literal_declarations = sorted(extra_action_keys - declared_action_literals)
    stale_action_literal_declarations = sorted(declared_action_literals - extra_action_keys)
    if missing_action_literal_declarations:
        failures.append(f"职业动作工厂仍有 {len(missing_action_literal_declarations)} 个字面量未分类")
    if stale_action_literal_declarations:
        failures.append(f"有 {len(stale_action_literal_declarations)} 个动作工厂字面量声明已与源码漂移")

    dynamic_calls = extract_dynamic_state_calls(simc_source)
    dynamic_keys = {
        (row["class"], row["source_file"], row["source_line"]) for row in dynamic_calls
    }
    declared_dynamic: dict[tuple[str, str, int], dict] = {}
    invalid_dynamic_declarations = []
    for index, declaration in enumerate(supplement_payload.get("dynamic_state_call_coverage") or []):
        class_name = str(declaration.get("class") or "").lower()
        source_file = str(declaration.get("source_file") or "")
        lines = declaration.get("source_lines") or []
        resolution = str(declaration.get("resolution") or "")
        tokens = declaration.get("tokens") or []
        if (not class_name or not source_file or not isinstance(lines, list) or
                not all(isinstance(line, int) and line > 0 for line in lines) or
                resolution not in {"covered_tokens", "helper_wrapper", "non_class_actor"} or
                not isinstance(tokens, list) or not all(isinstance(token, str) and token for token in tokens) or
                (resolution == "covered_tokens" and not tokens) or
                (resolution != "covered_tokens" and tokens)):
            invalid_dynamic_declarations.append(index)
            continue
        for line in lines:
            key = (class_name, source_file, line)
            if key in declared_dynamic:
                invalid_dynamic_declarations.append(index)
            declared_dynamic[key] = declaration
        if resolution == "covered_tokens":
            uncovered = [
                token for token in tokens
                if (class_name, token.lower()) not in runtime_states | supplement_state_tokens
            ]
            if uncovered:
                invalid_dynamic_declarations.append(index)
    missing_dynamic_declarations = sorted(dynamic_keys - set(declared_dynamic))
    stale_dynamic_declarations = sorted(set(declared_dynamic) - dynamic_keys)
    if invalid_dynamic_declarations:
        failures.append(f"有 {len(set(invalid_dynamic_declarations))} 组动态状态调用声明无效")
    if missing_dynamic_declarations:
        failures.append(f"仍有 {len(missing_dynamic_declarations)} 个动态状态构造调用未分类")
    if stale_dynamic_declarations:
        failures.append(f"有 {len(stale_dynamic_declarations)} 个动态状态调用声明已与源码漂移")

    invalid_sources = []
    for index, row in enumerate(supplements):
        source_file = simc_source / str(row.get("source_file") or "")
        evidence = str(row.get("dynamic_source_token") or row.get("token") or "")
        if not source_file.is_file() or evidence not in source_file.read_text(encoding="utf-8", errors="replace"):
            invalid_sources.append(index)
    if invalid_sources:
        failures.append(f"有 {len(invalid_sources)} 条源码补充无法在声明文件中定位")

    missing_package_definitions = []
    missing_chinese = []
    if package_payload is not None:
        facts = package_payload.get("facts") or []
        fact_keys = {
            (
                str(fact.get("class_name") or "").replace("deathknight", "death_knight").replace("demonhunter", "demon_hunter"),
                str(fact.get("symbol_kind") or ""),
                str(fact.get("token") or ""),
            )
            for fact in facts if (fact.get("metadata") or {}).get("source_coverage")
        }
        for row in supplements:
            key = (str(row.get("class") or ""), str(row.get("kind") or ""), str(row.get("token") or ""))
            if key not in fact_keys:
                missing_package_definitions.append(key)
        missing_chinese = [
            (fact.get("class_name"), fact.get("symbol_kind"), fact.get("token"))
            for fact in facts if not str(fact.get("name_zh") or "").strip()
        ]
        sentinels = {
            (fact.get("class_name"), fact.get("symbol_kind"), fact.get("token"))
            for fact in facts
        }
        for sentinel in (("warrior", "buff", "avatar"), ("warrior", "buff", "burst_of_power")):
            if sentinel not in sentinels:
                failures.append(f"最终包缺少哨兵字段 {sentinel[2]}")
        if missing_package_definitions:
            failures.append(f"有 {len(missing_package_definitions)} 条源码补充未进入最终包")
        if missing_chinese:
            failures.append(f"最终包仍有 {len(missing_chinese)} 条中文为空")

    return {
        "simc_revision": revision,
        "game_build": build,
        "counts": {
            "static_action_candidates": len(static_actions),
            "covered_static_actions": len(static_actions) - len(missing_actions),
            "action_factory_non_token_literals": len(extra_action_keys),
            "classified_action_factory_non_token_literals": (
                len(extra_action_keys) - len(missing_action_literal_declarations)
            ),
            "static_state_candidates": len(static_states),
            "covered_static_states": len(static_states) - len(missing_states),
            "dynamic_state_calls": len(dynamic_keys),
            "covered_dynamic_state_calls": len(dynamic_keys) - len(missing_dynamic_declarations),
            "supplement_definitions": len(supplements),
            "supplement_kinds": dict(sorted(Counter(str(row.get("kind") or "") for row in supplements).items())),
            "final_facts": len(package_payload.get("facts") or []) if package_payload else None,
        },
        "missing_actions": [{"class": item[0], "token": item[1]} for item in missing_actions],
        "missing_action_factory_literal_declarations": [
            {"class": item[0], "source_file": item[1], "source_line": item[2], "literal": item[3]}
            for item in missing_action_literal_declarations
        ],
        "stale_action_factory_literal_declarations": [
            {"class": item[0], "source_file": item[1], "source_line": item[2], "literal": item[3]}
            for item in stale_action_literal_declarations
        ],
        "missing_states": [{"class": item[0], "token": item[1]} for item in missing_states],
        "missing_dynamic_state_calls": [
            {"class": item[0], "source_file": item[1], "source_line": item[2]}
            for item in missing_dynamic_declarations
        ],
        "stale_dynamic_state_call_declarations": [
            {"class": item[0], "source_file": item[1], "source_line": item[2]}
            for item in stale_dynamic_declarations
        ],
        "invalid_dynamic_state_call_declaration_indexes": sorted(set(invalid_dynamic_declarations)),
        "invalid_source_definition_indexes": invalid_sources,
        "missing_package_definitions": [list(item) for item in missing_package_definitions],
        "missing_chinese": [list(item) for item in missing_chinese],
        "status": "ok" if not failures else "failed",
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计 SimC 源码支持的技能、Buff 和 Debuff 是否完整入库")
    parser.add_argument("--simc-source", type=Path, required=True, help="锁定 revision 的 SimC 源码目录")
    parser.add_argument("--source-json", type=Path, required=True, help="运行时字段清单 JSON")
    parser.add_argument("--supplements", type=Path, required=True, help="源码静态补充 JSON")
    parser.add_argument("--package", type=Path, help="最终数据库元数据包 JSON")
    parser.add_argument("--report", type=Path, help="可选的审计报告 JSON 输出路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = audit_source_coverage(
        args.simc_source.resolve(),
        _read_json(args.source_json.resolve()),
        _read_json(args.supplements.resolve()),
        _read_json(args.package.resolve()) if args.package else None,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
