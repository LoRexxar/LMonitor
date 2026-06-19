"""Utilities for rendering Blizzard spell description placeholders.

This is intentionally conservative: resolve the common placeholders used in
WoW spell/talent text, keep readable fallbacks when the exact DB2 value is
missing, and never raise from template rendering paths.
"""
from __future__ import annotations

import ast
import csv
import operator
import re
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


_VAR_RE = re.compile(r"\$(?:(?P<spell>\d+))?(?P<kind>[smAtdoUiL])(?P<idx>\d*)", re.IGNORECASE)
_EXPR_RE = re.compile(r"\$\{([^{}]+)\}")
_SPELLNAME_RE = re.compile(r"\$@spellname(\d+)", re.IGNORECASE)
_SPELLDESC_RE = re.compile(r"\$@spelldesc(\d+)", re.IGNORECASE)
_SPELLICON_RE = re.compile(r"\$@spellicon(\d+)", re.IGNORECASE)
_COND_RE = re.compile(r"\$\?[^\[]*\[([^\[\]]*)\]\[([^\[\]]*)\]")
_COND_ONE_RE = re.compile(r"\$\?[^\[]*\[([^\[\]]*)\]")
_NAMED_RE = re.compile(r"\$<([^>]+)>")


@dataclass
class SpellTextResolver:
    """Resolve placeholders in spell/talent text using DB snapshots.

    Snapshots are loaded lazily and cached per resolver. The class is safe to
    use from request rendering: database failures simply degrade to cleaned
    placeholder text.
    """

    locale: str = "zhCN"
    branch: str = "wow"
    _spell_cache: dict[int, dict[str, str]] = field(default_factory=dict)
    _effect_cache: dict[int, dict[int, dict[str, str]]] = field(default_factory=dict)
    _missing_spells: set[int] = field(default_factory=set)
    _missing_effects: set[int] = field(default_factory=set)

    def resolve(self, text: str | None, spell_id: int | None = None, *, depth: int = 0) -> str:
        text = text or ""
        if not text or "$" not in text:
            return self._cleanup(text)
        if depth > 3:
            return self._cleanup_unresolved(text)

        sid = _to_int(spell_id)
        try:
            out = text

            prev = None
            while prev != out:
                prev = out
                out = _COND_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), out)
                out = _COND_ONE_RE.sub(lambda m: m.group(1) or "", out)
            out = _SPELLNAME_RE.sub(lambda m: self._spell_name(_to_int(m.group(1))) or "", out)
            out = _SPELLDESC_RE.sub(
                lambda m: self.resolve(self._spell_desc(_to_int(m.group(1))), _to_int(m.group(1)), depth=depth + 1),
                out,
            )
            out = _SPELLICON_RE.sub('', out)
            # Expressions first, so ${$s3/1000}.1 becomes an evaluated value.
            out = _EXPR_RE.sub(lambda m: self._resolve_expr(m.group(1), sid), out)
            out = _VAR_RE.sub(lambda m: self._resolve_var_match(m, sid), out)
            out = _NAMED_RE.sub(lambda m: self._resolve_named(m.group(1), sid), out)
            return self._cleanup_unresolved(out)
        except Exception:
            return self._cleanup_unresolved(text)

    def _spell_snapshot(self, spell_id: int) -> dict[str, str]:
        spell_id = _to_int(spell_id)
        if spell_id <= 0:
            return {}
        if spell_id in self._spell_cache:
            return self._spell_cache[spell_id]
        if spell_id in self._missing_spells:
            return {}
        try:
            from botend.models import WowSpellSnapshot

            row = (
                WowSpellSnapshot.objects.filter(branch=self.branch, locale=self.locale, spell_id=spell_id)
                .order_by("-updated_at")
                .first()
            )
            if not row and self.locale != "enUS":
                row = (
                    WowSpellSnapshot.objects.filter(branch=self.branch, spell_id=spell_id)
                    .order_by("-updated_at")
                    .first()
                )
            if row:
                data = {
                    "name": getattr(row, "name", "") or "",
                    "name_zh": getattr(row, "name_zh", "") or "",
                    "description": getattr(row, "description", "") or "",
                    "aura_description": getattr(row, "aura_description", "") or "",
                }
                self._spell_cache[spell_id] = data
                return data
        except Exception:
            pass
        self._missing_spells.add(spell_id)
        return {}

    def _spell_name(self, spell_id: int) -> str:
        snap = self._spell_snapshot(spell_id)
        return snap.get("name_zh") or snap.get("name") or _csv_spell_name(spell_id) or (f"#{spell_id}" if spell_id else "")

    def _spell_desc(self, spell_id: int) -> str:
        snap = self._spell_snapshot(spell_id)
        return snap.get("description") or snap.get("aura_description") or _csv_spell_desc(spell_id) or ""

    def _effects(self, spell_id: int) -> dict[int, dict[str, str]]:
        spell_id = _to_int(spell_id)
        if spell_id <= 0:
            return {}
        if spell_id in self._effect_cache:
            return self._effect_cache[spell_id]
        if spell_id in self._missing_effects:
            return {}
        effects: dict[int, dict[str, str]] = {}
        try:
            from botend.models import WowSpellEffectSnapshot

            for row in WowSpellEffectSnapshot.objects.filter(branch=self.branch, locale=self.locale, spell_id=spell_id):
                idx = _to_int(getattr(row, "effect_index", 0))
                effects[idx] = {
                    "base_points": getattr(row, "base_points", "") or "",
                    "coefficient": getattr(row, "coefficient", "") or "",
                    "pvp_multiplier": getattr(row, "pvp_multiplier", "") or "",
                }
        except Exception:
            effects = {}
        if effects:
            self._effect_cache[spell_id] = effects
            return effects
        self._missing_effects.add(spell_id)
        return {}

    def _resolve_var_match(self, m: re.Match[str], current_spell_id: int) -> str:
        target_sid = _to_int(m.group("spell")) or current_spell_id
        kind = (m.group("kind") or "").lower()
        idx = _to_int(m.group("idx"))
        if idx <= 0:
            idx = 1
        val = self._effect_value(target_sid, idx, kind)
        if val != "":
            return val
        return m.group(0)

    def _effect_value(self, spell_id: int, idx: int, kind: str) -> str:
        effects = self._effects(spell_id)
        if not effects:
            return ""
        # Blizzard placeholders are usually 1-based while DB EffectIndex can be 0-based.
        # Only $s1 may safely fall back to effect index 0. For $s3, falling back
        # to effect 0 invents wrong numbers (e.g. 0%).
        row = effects.get(idx) or effects.get(idx - 1) or (effects.get(0) if idx == 1 else {}) or {}
        raw = row.get("base_points") or ""
        if raw == "":
            return ""
        num = _num(raw)
        if num is None:
            return str(raw)
        if kind in {'a', 'd', 't', 'o', 'u', 'i'}:
            # Radius / duration / tick period live in other DB2 tables.  Do not
            # substitute EffectBasePointsF for them; that produces wrong text.
            return ""
        if kind in {'m', 's'}:
            num = abs(num)
        return _fmt(num)

    def _resolve_expr(self, expr: str, current_spell_id: int) -> str:
        unresolved = False

        def repl(m):
            nonlocal unresolved
            val = self._resolve_var_match(m, current_spell_id)
            if not val or val.startswith('$'):
                unresolved = True
                return '0'
            return val

        replaced = _VAR_RE.sub(repl, expr)
        if unresolved:
            return '${' + expr + '}'
        replaced = replaced.replace(" ", "")
        if not re.fullmatch(r"[0-9+\-*/().]+", replaced):
            return '${' + expr + '}'
        try:
            val = _safe_eval(replaced)
            return _fmt(val)
        except Exception:
            return '${' + expr + '}'

    def _resolve_named(self, name: str, current_spell_id: int) -> str:
        # Complex named variables require game formulas not present in snapshots.
        # Hide them rather than showing raw "$<absorb>" in tooltips.
        return ""

    @staticmethod
    def _cleanup(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _cleanup_unresolved(self, text: str) -> str:
        text = text or ""
        prev = None
        while prev != text:
            prev = text
            text = _COND_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), text)
            text = _COND_ONE_RE.sub(lambda m: m.group(1) or "", text)
        text = _SPELLNAME_RE.sub(lambda m: self._spell_name(_to_int(m.group(1))) or "", text)
        text = _SPELLDESC_RE.sub("", text)
        text = _SPELLICON_RE.sub("", text)
        text = _EXPR_RE.sub("若干", text)
        text = _VAR_RE.sub(_readable_unresolved_var, text)
        # Some Blizzard client conditionals are stored as ?c3[...][] after the
        # leading "$" was stripped by earlier resolution passes.  Clean both
        # two-branch and one-branch forms so tooltips never expose raw tokens.
        bare_cond_two = re.compile(r"\?c\d+\[([^\[\]]*)\]\[([^\[\]]*)\]", re.IGNORECASE)
        bare_cond_one = re.compile(r"\?c\d+\[([^\[\]]*)\]")
        prev = None
        while prev != text:
            prev = text
            text = bare_cond_two.sub(lambda m: (m.group(1) or m.group(2) or ""), text)
            text = bare_cond_one.sub(lambda m: m.group(1) or "", text)
        text = _NAMED_RE.sub(lambda m: "若干" if (m.group(1) or "").strip() else "", text)
        text = re.sub(r"若干\.\d+", "若干", text)
        text = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", text)
        text = text.replace("..", ".")
        return self._cleanup(text)


def _readable_unresolved_var(m: re.Match[str]) -> str:
    kind = (m.group('kind') or '').lower()
    if kind == 'd':
        return '一段时间'
    if kind == 'a':
        return '一定'
    if kind == 't':
        return '一段时间'
    if kind == 'o':
        return '若干'
    if kind == 'u':
        return '若干'
    if kind == 'i':
        return '若干'
    return '若干'


@lru_cache(maxsize=8)
def get_spell_text_resolver(locale: str = "zhCN", branch: str = "wow") -> SpellTextResolver:
    return SpellTextResolver(locale=locale, branch=branch)


def resolve_spell_text(text: str | None, spell_id: int | None = None, *, locale: str = "zhCN", branch: str = "wow") -> str:
    return get_spell_text_resolver(locale, branch).resolve(text, spell_id)


def _dump_file(filename: str) -> Path:
    local = Path('.cache/wago_db2_dumps/latest') / filename
    if local.exists():
        return local
    try:
        from django.conf import settings

        base = Path(getattr(settings, 'BASE_DIR', Path.cwd()))
        candidate = base / '.cache' / 'wago_db2_dumps' / 'latest' / filename
        if candidate.exists():
            return candidate
    except Exception:
        pass
    return local


@lru_cache(maxsize=1)
def _csv_spell_names() -> dict[int, str]:
    path = _dump_file('SpellName_zhCN.csv')
    out: dict[int, str] = {}
    if not path.exists():
        return out
    try:
        with path.open(encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sid = _to_int(row.get('ID'))
                name = (row.get('Name_lang') or '').strip()
                if sid and name:
                    out[sid] = name
    except Exception:
        return {}
    return out


@lru_cache(maxsize=1)
def _csv_spell_descs() -> dict[int, str]:
    path = _dump_file('Spell_zhCN.csv')
    out: dict[int, str] = {}
    if not path.exists():
        return out
    try:
        with path.open(encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sid = _to_int(row.get('ID'))
                desc = (row.get('Description_lang') or row.get('AuraDescription_lang') or '').strip()
                if sid and desc:
                    out[sid] = desc
    except Exception:
        return {}
    return out


def _csv_spell_name(spell_id: int) -> str:
    return _csv_spell_names().get(_to_int(spell_id), '')


def _csv_spell_desc(spell_id: int) -> str:
    return _csv_spell_descs().get(_to_int(spell_id), '')


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip() or "0")
    except Exception:
        return 0


def _num(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _fmt(value: float | int) -> str:
    try:
        v = float(value)
    except Exception:
        return str(value)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return (f"{v:.2f}").rstrip("0").rstrip(".")


_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(expr: str) -> float:
    node = ast.parse(expr, mode="eval")

    def walk(n):
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in _ALLOWED_BINOPS:
            return _ALLOWED_BINOPS[type(n.op)](walk(n.left), walk(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(n.op)](walk(n.operand))
        raise ValueError("unsupported expression")

    return float(walk(node))
