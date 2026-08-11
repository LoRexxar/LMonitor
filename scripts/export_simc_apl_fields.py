#!/usr/bin/env python3
"""从锁定的 SimC 源码与运行时 manifest 生成 APL 技能/Buff 简中清单。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests


SIMC_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
ACTION_FUNCTION_RE = re.compile(
    r"\baction_t\s*\*\s*"
    r"(?P<owner>[A-Za-z_][A-Za-z0-9_:]*::)"
    r"(?P<function>create_action[A-Za-z0-9_]*)\s*"
    r"\((?P<params>[^;{}]{0,700}?)\)\s*\{",
    re.DOTALL,
)
STRING_VIEW_PARAMETER_RE = re.compile(
    r"(?:(?:util|std)::)?string_view\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)

CLASS_OWNER_KEYS = {
    "death_knight_t": "death_knight",
    "demon_hunter_t": "demon_hunter",
    "druid_t": "druid",
    "evoker_t": "evoker",
    "hunter_t": "hunter",
    "mage_t": "mage",
    "monk_t": "monk",
    "paladin_t": "paladin",
    "priest_t": "priest",
    "rogue_t": "rogue",
    "shaman_t": "shaman",
    "warlock_t": "warlock",
    "warrior_t": "warrior",
}

APL_FILE_CLASS_KEYS = {
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

CLASS_ZH = {
    "death_knight": "死亡骑士",
    "demon_hunter": "恶魔猎手",
    "druid": "德鲁伊",
    "evoker": "唤魔师",
    "hunter": "猎人",
    "mage": "法师",
    "monk": "武僧",
    "paladin": "圣骑士",
    "priest": "牧师",
    "rogue": "潜行者",
    "shaman": "萨满祭司",
    "warlock": "术士",
    "warrior": "战士",
}

SPEC_ZH = {
    ("death_knight", "blood"): "鲜血",
    ("death_knight", "frost"): "冰霜",
    ("death_knight", "unholy"): "邪恶",
    ("demon_hunter", "devourer"): "吞噬",
    ("demon_hunter", "havoc"): "浩劫",
    ("demon_hunter", "vengeance"): "复仇",
    ("druid", "balance"): "平衡",
    ("druid", "feral"): "野性",
    ("druid", "guardian"): "守护",
    ("druid", "restoration"): "恢复",
    ("evoker", "augmentation"): "增辉",
    ("evoker", "devastation"): "湮灭",
    ("hunter", "beast_mastery"): "野兽控制",
    ("hunter", "marksmanship"): "射击",
    ("hunter", "survival"): "生存",
    ("mage", "arcane"): "奥术",
    ("mage", "fire"): "火焰",
    ("mage", "frost"): "冰霜",
    ("monk", "brewmaster"): "酒仙",
    ("monk", "windwalker"): "踏风",
    ("paladin", "protection"): "防护",
    ("paladin", "retribution"): "惩戒",
    ("priest", "shadow"): "暗影",
    ("rogue", "assassination"): "奇袭",
    ("rogue", "outlaw"): "狂徒",
    ("rogue", "subtlety"): "敏锐",
    ("shaman", "elemental"): "元素",
    ("shaman", "enhancement"): "增强",
    ("warlock", "affliction"): "痛苦",
    ("warlock", "demonology"): "恶魔学识",
    ("warlock", "destruction"): "毁灭",
    ("warrior", "arms"): "武器",
    ("warrior", "fury"): "狂怒",
    ("warrior", "protection"): "防护",
}

KIND_ZH = {
    "action": "技能动作",
    "buff": "增益",
    "debuff": "减益",
    "dot": "持续效果",
    "cooldown": "冷却引用",
}

KIND_PREFIX = {
    "action": "",
    "buff": "buff.",
    "debuff": "debuff.",
    "dot": "dot.",
    "cooldown": "cooldown.",
}

SPEC_ALIASES = {
    "beastmastery": "beast_mastery",
}

TRUSTED_ACTION_SOURCES = {
    "runtime_apl_action",
    "runtime_action_probe",
}


@dataclass(frozen=True)
class StaticActionCandidate:
    class_name: str
    token: str
    function: str
    source_file: str
    source_line: int


@dataclass
class FieldRecord:
    class_name: str
    spec: str
    kind: str
    token: str
    spell_ids: set[int] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    aliases: set[str] = field(default_factory=set)
    options: set[str] = field(default_factory=set)

    @property
    def spell_id(self) -> int | None:
        return next(iter(self.spell_ids)) if len(self.spell_ids) == 1 else None

    @property
    def identity_status(self) -> str:
        if len(self.spell_ids) == 1:
            return "已绑定"
        if len(self.spell_ids) > 1:
            return "Spell ID 歧义"
        return "未绑定"

    @property
    def apl_field(self) -> str:
        return f"{KIND_PREFIX[self.kind]}{self.token}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 SimC 源码、运行时 manifest 与 Wowhead 生成 APL 字段简中清单"
    )
    parser.add_argument("--simc-source", type=Path, required=True, help="锁定的 SimC 源码目录")
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        default=[],
        help="patched SimC 运行时 manifest；可重复传入",
    )
    parser.add_argument("--probe-output", type=Path, help="写出按职业分组的 action 探针 TSV")
    parser.add_argument("--probe-index", type=Path, help="逐专精 action 探针结果索引 JSON")
    parser.add_argument("--output-csv", type=Path, help="最终中文 CSV")
    parser.add_argument("--unique-output-csv", type=Path, help="按 Spell ID 去重的简中 CSV")
    parser.add_argument("--output-json", type=Path, help="最终机器可读 JSON")
    parser.add_argument("--summary-output", type=Path, help="中文覆盖率摘要 Markdown")
    parser.add_argument("--wowhead-cache", type=Path, help="Wowhead 简中缓存 JSON")
    parser.add_argument("--fetch-wowhead", action="store_true", help="为已绑定 Spell ID 抓取 Wowhead 简中")
    parser.add_argument("--refresh-failed", action="store_true", help="重试缓存中的失败/空记录")
    parser.add_argument("--workers", type=int, default=6, help="Wowhead 请求并发数")
    parser.add_argument("--delay", type=float, default=0.05, help="每个请求前的延迟秒数")
    parser.add_argument("--data-env", type=int, default=1, help="Wowhead 环境，正式服为 1")
    parser.add_argument("--locale", type=int, default=4, help="Wowhead 语言，简中为 4")
    return parser.parse_args()


def line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def matching_brace(text: str, open_index: int) -> int:
    """在忽略注释和字符串的前提下找到函数体右花括号。"""
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
            elif char == "{":
                depth += 1
            elif char == "}":
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
    raise ValueError(f"找不到从偏移 {open_index} 开始的函数体结束位置")


def comparison_tokens(body: str, parameter: str) -> set[str]:
    escaped = re.escape(parameter)
    patterns = (
        re.compile(rf"\b{escaped}\s*==\s*\"([a-z0-9_]+)\""),
        re.compile(rf"\"([a-z0-9_]+)\"\s*==\s*\b{escaped}\b"),
        re.compile(
            rf"(?:util::)?str_compare_ci\s*\(\s*{escaped}\s*,\s*\"([a-z0-9_]+)\""
        ),
        re.compile(
            rf"(?:util::)?str_compare_ci\s*\(\s*\"([a-z0-9_]+)\"\s*,\s*{escaped}"
        ),
    )
    result: set[str] = set()
    for pattern in patterns:
        result.update(match.group(1) for match in pattern.finditer(body))
    return result


def extract_static_action_candidates(simc_source: Path) -> list[StaticActionCandidate]:
    engine = simc_source / "engine"
    candidates: dict[tuple[str, str, str, str, int], StaticActionCandidate] = {}
    for path in sorted((*engine.rglob("*.cpp"), *engine.rglob("*.hpp"))):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        for match in ACTION_FUNCTION_RE.finditer(text):
            owner = match.group("owner").rstrip(":").split("::")[-1]
            class_name = CLASS_OWNER_KEYS.get(owner)
            if not class_name:
                continue
            parameters = list(STRING_VIEW_PARAMETER_RE.finditer(match.group("params")))
            if not parameters:
                continue
            action_parameter = parameters[0].group("name")
            open_index = match.end() - 1
            end_index = matching_brace(text, open_index)
            body = text[open_index + 1 : end_index]
            function = match.group("function")
            source_file = path.relative_to(simc_source).as_posix()
            source_line = line_number(text, match.start())
            for token in comparison_tokens(body, action_parameter):
                key = (class_name, token, function, source_file, source_line)
                candidates[key] = StaticActionCandidate(*key)
    return sorted(
        candidates.values(),
        key=lambda item: (item.class_name, item.token, item.source_file, item.source_line),
    )


def extract_expression_suffixes(simc_source: Path) -> dict[str, list[str]]:
    """从当前锁定源码提取 Buff、DoT 与冷却表达式支持的属性后缀。"""
    targets = {
        "buff": (
            simc_source / "engine" / "buff" / "buff.cpp",
            re.compile(r"\bcreate_buff_expression\s*\([^;{}]{0,900}\)\s*\{"),
            "type",
        ),
        "dot": (
            simc_source / "engine" / "action" / "dot.cpp",
            re.compile(r"\bdot_t::create_expression\s*\([^;{}]{0,900}\)\s*\{"),
            "name_str",
        ),
        "cooldown": (
            simc_source / "engine" / "sim" / "cooldown.cpp",
            re.compile(r"\bcooldown_t::create_expression\s*\([^;{}]{0,900}\)\s*\{"),
            "name",
        ),
    }
    result: dict[str, list[str]] = {"action": []}
    for kind, (path, signature, parameter) in targets.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        match = signature.search(text)
        if not match:
            raise ValueError(f"无法在 {path} 定位 {kind} 表达式解析函数")
        open_index = match.end() - 1
        body = text[open_index + 1 : matching_brace(text, open_index)]
        suffix_pattern = re.compile(
            rf"\b{re.escape(parameter)}\s*==\s*\"([a-z0-9_]+)\""
        )
        result[kind] = list(dict.fromkeys(suffix_pattern.findall(body)))
        if not result[kind]:
            raise ValueError(f"无法在 {path} 提取 {kind} 表达式属性")
    result["debuff"] = list(result["buff"])
    return result


def official_specs(simc_source: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    apl_dir = simc_source / "ActionPriorityLists" / "default"
    for path in sorted(apl_dir.glob("*.simc")):
        stem = path.stem.lower()
        prefix, separator, spec = stem.partition("_")
        class_name = APL_FILE_CLASS_KEYS.get(prefix)
        if not separator or not class_name or not spec:
            continue
        result[class_name].add(SPEC_ALIASES.get(spec, spec))
    return dict(result)


def write_probe_file(path: Path, candidates: Iterable[StaticActionCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted({(item.class_name, item.token) for item in candidates})
    path.write_text("".join(f"{class_name}\t{token}\n" for class_name, token in rows), encoding="utf-8")


def load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"{path} 不是受支持的 SimC APL manifest")
    revision = str(payload.get("simc_revision") or "").lower()
    if not SIMC_REVISION_RE.fullmatch(revision):
        raise ValueError(f"{path} 缺少有效的 40 位 SimC revision")
    if not isinstance(payload.get("symbols"), list):
        raise ValueError(f"{path} 缺少 symbols 数组")
    return payload


def load_probe_index(path: Path | None, revision: str, game_build: str) -> dict:
    """读取逐专精探针索引，并压缩成可审计的结果摘要。"""
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"{path} 不是受支持的 action 探针索引")
    if str(payload.get("simc_revision") or "").lower() != revision:
        raise ValueError(f"{path} 的 SimC revision 与 manifest 不一致")
    if str(payload.get("game_build") or "") != game_build:
        raise ValueError(f"{path} 的 game build 与 manifest 不一致")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError(f"{path} 缺少 results 数组")
    statuses = Counter(str(result.get("status") or "unknown") for result in results)
    unsafe_tokens = sorted({
        (str(result.get("class") or ""), str(token))
        for result in results
        for token in result.get("unsafe_tokens") or []
        if str(result.get("class") or "") and str(token)
    })
    return {
        "path": str(path.resolve()),
        "spec_count": len(results),
        "status_counts": dict(sorted(statuses.items())),
        "unsafe_tokens": [
            {"class": class_name, "token": token}
            for class_name, token in unsafe_tokens
        ],
    }


def merge_manifests(
    paths: list[Path], supported: dict[str, set[str]]
) -> tuple[list[FieldRecord], dict]:
    records: dict[tuple[str, str, str, str], FieldRecord] = {}
    revisions: set[str] = set()
    builds: set[str] = set()
    manifests = []
    for path in paths:
        payload = load_manifest(path)
        revisions.add(str(payload["simc_revision"]).lower())
        builds.add(str(payload.get("game_build") or ""))
        manifests.append({
            "path": str(path.resolve()),
            "symbol_count": len(payload["symbols"]),
            "completeness": payload.get("completeness") or {},
        })
        for symbol in payload["symbols"]:
            if not isinstance(symbol, dict):
                continue
            kind = str(symbol.get("kind") or "").strip().lower()
            if kind not in KIND_ZH:
                continue
            raw_class_name = str(symbol.get("class") or "").strip().lower()
            # SimC 运行时使用 deathknight/demonhunter，而默认 APL 文件和
            # 本导出器统一使用 death_knight/demon_hunter；合并前必须归一化。
            class_name = APL_FILE_CLASS_KEYS.get(raw_class_name, raw_class_name)
            spec = SPEC_ALIASES.get(str(symbol.get("spec") or "").strip().lower(),
                                    str(symbol.get("spec") or "").strip().lower())
            token = str(symbol.get("token") or "").strip().lower()
            if not class_name or not spec or not token:
                continue
            if spec not in supported.get(class_name, set()):
                continue
            key = (class_name, spec, kind, token)
            record = records.setdefault(key, FieldRecord(*key))
            spell_id = symbol.get("spell_id")
            if isinstance(spell_id, int) and not isinstance(spell_id, bool) and spell_id > 0:
                record.spell_ids.add(spell_id)
            for candidate in symbol.get("candidates") or []:
                if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                    record.spell_ids.add(candidate)
            source = str(symbol.get("source") or "").strip()
            if source:
                record.sources.add(source)
            reason = str(symbol.get("reason") or "").strip()
            if reason:
                record.reasons.add(reason)
            record.aliases.update(
                str(value).strip().lower()
                for value in symbol.get("aliases") or []
                if str(value).strip()
            )
            record.options.update(
                str(value).strip().lower()
                for value in symbol.get("options") or []
                if str(value).strip()
            )
    if len(revisions) != 1:
        raise ValueError(f"manifest 的 SimC revision 不一致: {sorted(revisions)}")
    if len(builds) != 1:
        raise ValueError(f"manifest 的 game build 不一致: {sorted(builds)}")

    filtered = []
    for record in records.values():
        if record.kind == "action" and not (record.sources & TRUSTED_ACTION_SOURCES):
            continue
        filtered.append(record)
    filtered.sort(key=lambda item: (
        list(CLASS_ZH).index(item.class_name) if item.class_name in CLASS_ZH else 999,
        item.spec,
        list(KIND_ZH).index(item.kind),
        item.token,
    ))
    metadata = {
        "simc_revision": next(iter(revisions)),
        "game_build": next(iter(builds)),
        "manifests": manifests,
    }
    return filtered, metadata


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_wowhead_cache(path: Path | None, data_env: int, locale: int) -> dict:
    if path is None or not path.exists():
        return {
            "schema_version": 1,
            "data_env": data_env,
            "locale": locale,
            "records": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or int(payload.get("data_env", -1)) != data_env
        or int(payload.get("locale", -1)) != locale
        or not isinstance(payload.get("records"), dict)
    ):
        raise ValueError(f"Wowhead 缓存上下文与 dataEnv={data_env}, locale={locale} 不一致")
    return payload


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def fetch_wowhead_record(
    spell_id: int,
    *,
    data_env: int,
    locale: int,
    delay: float,
    retries: int = 2,
) -> tuple[int, dict]:
    if delay:
        time.sleep(delay)
    url = f"https://nether.wowhead.com/tooltip/spell/{spell_id}"
    last_error = ""
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params={"dataEnv": data_env, "locale": locale},
                timeout=(8, 35),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
                        "Gecko/20100101 Firefox/128.0"
                    ),
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            if response.status_code == 404:
                return spell_id, {
                    "status": "missing",
                    "name_zh": "",
                    "raw_name": "",
                    "fetched_at": utc_now(),
                }
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Tooltip 响应不是对象")
            raw_name = str(payload.get("name") or "").strip()
            name_zh = raw_name if CJK_RE.search(raw_name) else ""
            return spell_id, {
                "status": "ok" if name_zh else ("unlocalized" if raw_name else "empty"),
                "name_zh": name_zh,
                "raw_name": raw_name,
                "icon": str(payload.get("icon") or "").strip(),
                "fetched_at": utc_now(),
            }
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()[:400]
            if attempt < retries:
                time.sleep(2**attempt)
    return spell_id, {
        "status": "request_failed",
        "name_zh": "",
        "raw_name": "",
        "error": last_error,
        "fetched_at": utc_now(),
    }


def resolve_wowhead_names(
    spell_ids: set[int],
    *,
    cache_path: Path | None,
    data_env: int,
    locale: int,
    fetch: bool,
    refresh_failed: bool,
    workers: int,
    delay: float,
) -> dict[str, dict]:
    cache = load_wowhead_cache(cache_path, data_env, locale)
    records = cache["records"]
    retry_statuses = {"request_failed", "empty", "unlocalized"}
    missing = sorted(
        spell_id
        for spell_id in spell_ids
        if str(spell_id) not in records
        or (refresh_failed and records[str(spell_id)].get("status") in retry_statuses)
    )
    if fetch and missing:
        print(
            f"抓取 Wowhead 简中名称: total={len(spell_ids)} cached={len(spell_ids) - len(missing)} "
            f"missing={len(missing)} workers={max(1, workers)}",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    fetch_wowhead_record,
                    spell_id,
                    data_env=data_env,
                    locale=locale,
                    delay=max(0.0, delay),
                ): spell_id
                for spell_id in missing
            }
            stats: Counter[str] = Counter()
            for index, future in enumerate(as_completed(futures), start=1):
                spell_id, record = future.result()
                records[str(spell_id)] = record
                stats[record["status"]] += 1
                if cache_path and (index % 50 == 0 or index == len(futures)):
                    cache["updated_at"] = utc_now()
                    atomic_write_json(cache_path, cache)
                if index % 50 == 0 or index == len(futures):
                    summary = ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
                    print(f"Wowhead 进度 {index}/{len(futures)}: {summary}", flush=True)
    elif missing:
        print(f"Wowhead 缓存仍缺少 {len(missing)} 个 Spell ID；未启用 --fetch-wowhead", file=sys.stderr)
    if cache_path and not cache_path.exists():
        cache["updated_at"] = utc_now()
        atomic_write_json(cache_path, cache)
    return records


def record_to_dict(
    record: FieldRecord,
    wowhead: dict[str, dict],
    expression_suffixes: dict[str, list[str]],
) -> dict:
    spell_id = record.spell_id
    localized = wowhead.get(str(spell_id), {}) if spell_id else {}
    name_zh = str(localized.get("name_zh") or "")
    suffixes = expression_suffixes.get(record.kind, [])
    return {
        "class": record.class_name,
        "class_zh": CLASS_ZH.get(record.class_name, record.class_name),
        "spec": record.spec,
        "spec_zh": SPEC_ZH.get((record.class_name, record.spec), record.spec),
        "kind": record.kind,
        "kind_zh": KIND_ZH[record.kind],
        "apl_field": record.apl_field,
        "apl_expression_template": (
            f"{record.apl_field}.<属性>" if suffixes else record.apl_field
        ),
        "expression_suffixes": suffixes,
        "token": record.token,
        "spell_id": spell_id,
        "spell_id_candidates": sorted(record.spell_ids),
        "identity_status": record.identity_status,
        "name_zh": name_zh,
        "wowhead_raw_name": str(localized.get("raw_name") or ""),
        "wowhead_status": str(localized.get("status") or ("not_requested" if spell_id else "unbound")),
        "wowhead_url": f"https://www.wowhead.com/cn/spell={spell_id}" if spell_id else "",
        "sources": sorted(record.sources),
        "identity_reasons": sorted(record.reasons),
        "aliases": sorted(record.aliases),
        "action_options": sorted(record.options),
    }


CSV_FIELDS = (
    "职业",
    "专精",
    "类型",
    "APL字段",
    "APL表达式模板",
    "支持属性",
    "SimC Token",
    "Spell ID",
    "候选 Spell ID",
    "中文名称",
    "Wowhead原始名称",
    "身份状态",
    "Wowhead状态",
    "Wowhead链接",
    "运行时来源",
    "别名",
    "动作参数",
    "诊断",
)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "职业": row["class_zh"],
                "专精": row["spec_zh"],
                "类型": row["kind_zh"],
                "APL字段": row["apl_field"],
                "APL表达式模板": row["apl_expression_template"],
                "支持属性": ",".join(row["expression_suffixes"]),
                "SimC Token": row["token"],
                "Spell ID": row["spell_id"] or "",
                "候选 Spell ID": ",".join(map(str, row["spell_id_candidates"])),
                "中文名称": row["name_zh"],
                "Wowhead原始名称": row["wowhead_raw_name"],
                "身份状态": row["identity_status"],
                "Wowhead状态": row["wowhead_status"],
                "Wowhead链接": row["wowhead_url"],
                "运行时来源": ",".join(row["sources"]),
                "别名": ",".join(row["aliases"]),
                "动作参数": ",".join(row["action_options"]),
                "诊断": ",".join(row["identity_reasons"]),
            })


UNIQUE_CSV_FIELDS = (
    "Spell ID",
    "中文名称",
    "Wowhead原始名称",
    "Wowhead状态",
    "职业",
    "专精",
    "类型",
    "APL字段",
    "APL表达式模板",
    "Wowhead链接",
)


def write_unique_csv(path: Path, rows: list[dict]) -> None:
    """按 Spell ID 生成一对一中文映射，同时保留反向字段集合。"""
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        if row["spell_id"] is not None:
            grouped[row["spell_id"]].append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=UNIQUE_CSV_FIELDS)
        writer.writeheader()
        for spell_id, items in sorted(grouped.items()):
            first = items[0]
            writer.writerow({
                "Spell ID": spell_id,
                "中文名称": first["name_zh"],
                "Wowhead原始名称": first["wowhead_raw_name"],
                "Wowhead状态": first["wowhead_status"],
                "职业": ",".join(sorted({item["class_zh"] for item in items})),
                "专精": ",".join(sorted({item["spec_zh"] for item in items})),
                "类型": ",".join(sorted({item["kind_zh"] for item in items})),
                "APL字段": ",".join(sorted({item["apl_field"] for item in items})),
                "APL表达式模板": ",".join(sorted({
                    item["apl_expression_template"] for item in items
                })),
                "Wowhead链接": first["wowhead_url"],
            })


def write_summary(
    path: Path,
    rows: list[dict],
    metadata: dict,
    supported: dict[str, set[str]],
    candidates: list[StaticActionCandidate],
    probe_summary: dict,
    expression_suffixes: dict[str, list[str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = len(rows)
    bound = sum(row["spell_id"] is not None for row in rows)
    localized = sum(bool(row["name_zh"]) for row in rows)
    unique_spell_ids = {row["spell_id"] for row in rows if row["spell_id"] is not None}
    unique_localized_ids = {
        row["spell_id"] for row in rows if row["spell_id"] is not None and row["name_zh"]
    }
    kind_counts = Counter(row["kind_zh"] for row in rows)
    lines = [
        "# SimC APL 职业技能与 Buff 字段覆盖摘要",
        "",
        f"- SimC revision：`{metadata['simc_revision']}`",
        f"- SimC game build：`{metadata['game_build']}`",
        f"- 官方默认 APL 专精数：{sum(len(values) for values in supported.values())}",
        f"- 静态动作候选数：{len({(item.class_name, item.token) for item in candidates})}",
        f"- 最终字段行数：{total}",
        f"- 已绑定 Spell ID 的字段行：{bound}（{bound / max(1, total):.1%}）",
        f"- 唯一 Spell ID：{len(unique_spell_ids)}",
        f"- 已取得简中的字段行：{localized}（{localized / max(1, total):.1%}）",
        f"- 已取得简中的唯一 Spell ID：{len(unique_localized_ids)}",
        "",
        "## 类型统计",
        "",
        "| 类型 | 行数 |",
        "|---|---:|",
    ]
    lines.extend(f"| {kind} | {count} |" for kind, count in sorted(kind_counts.items()))
    lines.extend([
        "",
        "## 职业/专精覆盖",
        "",
        "| 职业 | 专精 | 字段 | 已绑定 | 简中 |",
        "|---|---|---:|---:|---:|",
    ])
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["class"], row["spec"])].append(row)
    for (class_name, spec), items in sorted(grouped.items()):
        lines.append(
            f"| {CLASS_ZH.get(class_name, class_name)} | "
            f"{SPEC_ZH.get((class_name, spec), spec)} | {len(items)} | "
            f"{sum(item['spell_id'] is not None for item in items)} | "
            f"{sum(bool(item['name_zh']) for item in items)} |"
        )
    lines.extend([
        "",
        "## 口径说明",
        "",
        "- 技能动作只收录官方 APL 实际调用或经编译后职业 `create_action` 探针成功构造的 token；内部伤害子动作不作为可调用技能列出。",
        "- Buff 来自 actor 的实际 Buff 注册表及条件 fallback 注册名；未选择天赋下的 fallback 可能没有 Spell ID，但字段本身仍可被 APL 解析。",
        "- Wowhead Tooltip 是正式服当前环境的简中值，不能锁定到具体历史 build；抓取时间保存在缓存中。",
        "- 动态装备、饰品和运行时格式化字段无法仅凭固定职业 profile 穷举，必须按具体装备/配置重新导出。",
        "",
        "## 表达式属性",
        "",
        "- Buff/减益：" + "、".join(f"`{value}`" for value in expression_suffixes["buff"]),
        "- DoT：" + "、".join(f"`{value}`" for value in expression_suffixes["dot"]),
        "- 冷却：" + "、".join(f"`{value}`" for value in expression_suffixes["cooldown"]),
    ])
    if probe_summary:
        lines.extend([
            "- 逐专精探针采用独立 SimC 进程；会触发底层异常的静态候选已二分隔离，不会让其他字段丢失。",
            "",
            "## 探针隔离结果",
            "",
            f"- 已完成专精数：{probe_summary['spec_count']}",
            "- 状态统计：" + "，".join(
                f"`{key}`={value}" for key, value in probe_summary["status_counts"].items()
            ),
        ])
        unsafe_tokens = probe_summary.get("unsafe_tokens") or []
        if unsafe_tokens:
            lines.append("- 已隔离候选：" + "，".join(
                f"`{item['class']}.{item['token']}`" for item in unsafe_tokens
            ))
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    simc_source = args.simc_source.resolve()
    if not (simc_source / "engine").is_dir():
        raise SystemExit(f"不是有效的 SimC 源码目录: {simc_source}")
    candidates = extract_static_action_candidates(simc_source)
    expression_suffixes = extract_expression_suffixes(simc_source)
    supported = official_specs(simc_source)
    if args.probe_output:
        write_probe_file(args.probe_output, candidates)
        counts = Counter(item.class_name for item in {(c.class_name, c.token): c for c in candidates}.values())
        print(
            f"已写 action 探针 {args.probe_output}: "
            + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        )
    if not args.manifest:
        return 0
    if not args.output_csv or not args.output_json or not args.summary_output:
        raise SystemExit("传入 --manifest 时必须同时指定 --output-csv/--output-json/--summary-output")
    records, metadata = merge_manifests(args.manifest, supported)
    probe_summary = load_probe_index(
        args.probe_index,
        metadata["simc_revision"],
        metadata["game_build"],
    )
    spell_ids = {record.spell_id for record in records if record.spell_id}
    wowhead = resolve_wowhead_names(
        spell_ids,
        cache_path=args.wowhead_cache,
        data_env=args.data_env,
        locale=args.locale,
        fetch=args.fetch_wowhead,
        refresh_failed=args.refresh_failed,
        workers=args.workers,
        delay=args.delay,
    )
    rows = [record_to_dict(record, wowhead, expression_suffixes) for record in records]
    unique_spell_ids = {row["spell_id"] for row in rows if row["spell_id"] is not None}
    unique_localized_ids = {
        row["spell_id"] for row in rows if row["spell_id"] is not None and row["name_zh"]
    }
    write_csv(args.output_csv, rows)
    if args.unique_output_csv:
        write_unique_csv(args.unique_output_csv, rows)
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        **metadata,
        "wowhead": {
            "data_env": args.data_env,
            "locale": args.locale,
            "environment_scope": "current",
            "cache_path": str(args.wowhead_cache.resolve()) if args.wowhead_cache else "",
        },
        "coverage": {
            "field_rows": len(rows),
            "bound_field_rows": sum(row["spell_id"] is not None for row in rows),
            "localized_field_rows": sum(bool(row["name_zh"]) for row in rows),
            "unique_spell_ids": len(unique_spell_ids),
            "localized_unique_spell_ids": len(unique_localized_ids),
            "wowhead_status_counts": dict(sorted(Counter(
                row["wowhead_status"] for row in rows if row["spell_id"] is not None
            ).items())),
        },
        "probe_summary": probe_summary,
        "expression_suffixes": expression_suffixes,
        "official_specs": {
            class_name: sorted(specs) for class_name, specs in sorted(supported.items())
        },
        "static_action_candidates": [
            {
                "class": item.class_name,
                "token": item.token,
                "function": item.function,
                "source_file": item.source_file,
                "source_line": item.source_line,
            }
            for item in candidates
        ],
        "records": rows,
    }
    atomic_write_json(args.output_json, payload)
    write_summary(
        args.summary_output,
        rows,
        metadata,
        supported,
        candidates,
        probe_summary,
        expression_suffixes,
    )
    print(
        f"生成完成: rows={len(rows)} bound={sum(row['spell_id'] is not None for row in rows)} "
        f"zh={sum(bool(row['name_zh']) for row in rows)} unique_spell_ids={len(spell_ids)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
