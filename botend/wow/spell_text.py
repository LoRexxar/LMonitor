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


_VAR_RE = re.compile(r"\$(?:(?P<spell>\d+))?(?P<kind>[smAtdoUiLrnchxb])(?P<idx>\d*)", re.IGNORECASE)
_INLINE_DIV_VAR_RE = re.compile(r"\$/((?P<divisor>\d+));(?:(?P<spell>\d+))?(?P<kind>[smAtdoUiLrnchxb])(?P<idx>\d*)", re.IGNORECASE)
_EXPR_RE = re.compile(r"\$\{([^{}]+)\}")
_SPELLNAME_RE = re.compile(r"\$@spellname(\d+)", re.IGNORECASE)
_SPELLDESC_RE = re.compile(r"\$@spelldesc(\d+)", re.IGNORECASE)
_SPELLTOOLTIP_RE = re.compile(r"\$@spelltooltip(\d+)", re.IGNORECASE)
_SPELLAURA_RE = re.compile(r"\$@spellaura(\d+)", re.IGNORECASE)
_SPELLICON_RE = re.compile(r"\$@spellicon(\d+)", re.IGNORECASE)
_COND_RE = re.compile(r"\$\?[^\[]*\[([^\[\]]*)\]\[([^\[\]]*)\]")
_COND_ONE_RE = re.compile(r"\$\?[^\[]*\[([^\[\]]*)\]")
_BARE_COND_RE = re.compile(r"\?(?:!?\$?[acs]\d+)(?:&!?\$?[acs]\d+)*\[([^\[\]]*)\]\[([^\[\]]*)\]", re.IGNORECASE)
_BARE_COND_ONE_RE = re.compile(r"\?(?:!?\$?[acs]\d+)(?:&!?\$?[acs]\d+)*\[([^\[\]]*)\]", re.IGNORECASE)
_SWITCH_RE = re.compile(r"\$@switch<[^>]*>\[([^\[\]]*)\]\[([^\[\]]*)\]", re.IGNORECASE)
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
    _duration_cache: dict[int, int] | None = None
    _radius_cache: dict[int, float] | None = None
    _misc_index_cache: dict[int, tuple[int, int]] | None = None

    def resolve(self, text: str | None, spell_id: int | None = None, *, depth: int = 0) -> str:
        text = text or ""
        if not text:
            return ""
        if "$" not in text and "?" not in text:
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
                out = _BARE_COND_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), out)
                out = _BARE_COND_ONE_RE.sub(lambda m: m.group(1) or "", out)
                out = _SWITCH_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), out)
            out = _SPELLNAME_RE.sub(lambda m: self._spell_name(_to_int(m.group(1))) or "", out)
            out = _SPELLDESC_RE.sub(
                lambda m: self.resolve(self._spell_desc(_to_int(m.group(1))), _to_int(m.group(1)), depth=depth + 1),
                out,
            )
            out = _SPELLTOOLTIP_RE.sub(
                lambda m: self.resolve(self._spell_desc(_to_int(m.group(1))), _to_int(m.group(1)), depth=depth + 1),
                out,
            )
            out = _SPELLAURA_RE.sub(
                lambda m: self.resolve(self._spell_aura(_to_int(m.group(1))), _to_int(m.group(1)), depth=depth + 1),
                out,
            )
            out = _SPELLICON_RE.sub('', out)
            # Expressions first, so ${$s3/1000}.1 becomes an evaluated value.
            out = _EXPR_RE.sub(lambda m: self._resolve_expr(m.group(1), sid), out)
            out = _INLINE_DIV_VAR_RE.sub(lambda m: self._resolve_inline_div_var_match(m, sid), out)
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

    def _spell_aura(self, spell_id: int) -> str:
        snap = self._spell_snapshot(spell_id)
        return snap.get("aura_description") or snap.get("description") or _csv_spell_desc(spell_id) or ""

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
        # Handle duration / aura-range / radius via DB2 reference tables
        if kind in ('d',):
            val = self._duration_value(target_sid, idx)
            if val != "":
                return val
        if kind in ('a', 'r'):
            val = self._radius_value(target_sid, idx)
            if val != "":
                return val
        # Fallback to SpellEffect-based resolution
        val = self._effect_value(target_sid, idx, kind)
        if val != "":
            return val
        return m.group(0)

    def _resolve_inline_div_var_match(self, m: re.Match[str], current_spell_id: int) -> str:
        target_sid = _to_int(m.group("spell")) or current_spell_id
        kind = (m.group("kind") or "").lower()
        idx = _to_int(m.group("idx"))
        if idx <= 0:
            idx = 1
        val = self._effect_value(target_sid, idx, kind)
        if val == "" or val.startswith('$'):
            return "x"
        divisor = _num(m.group("divisor"))
        num = _num(val)
        if divisor and num is not None:
            return _fmt(num / divisor)
        return val

    def _effect_value(self, spell_id: int, idx: int, kind: str) -> str:
        effects = self._effects(spell_id)
        if not effects:
            return ""
        # Blizzard placeholders are 1-based while DB2 EffectIndex is 0-based:
        # $s1 -> EffectIndex 0, $s2 -> EffectIndex 1.  Older code preferred
        # effects[idx] first, which turned $s1 into the second DB2 effect and
        # produced wrong values (for example 5% became 0%).
        row = effects.get(idx - 1) or effects.get(idx) or {}
        raw = row.get("base_points") or ""
        coeff = row.get("coefficient") or ""
        if raw == "" and coeff == "":
            return ""
        num = _num(raw)
        if num is None and not coeff:
            return str(raw)
        if kind in {'o', 'u', 't', 'i', 'n', 'c', 'h', 'x'}:
            # Periodic tick (o), max stacks (u), tick count (t), targets (i),
            # chain targets (n), cost (c), honor (h), client runtime (x)
            # live in other DB2 tables or game state.  Return "" so
            # _cleanup_unresolved replaces them with readable text.
            return ""
        # When base_points is 0 but coefficient is available, the real value
        # comes from coefficient × spell/attack power (runtime stat).
        # Show the coefficient as a percentage instead of flat "0".
        if num is not None and num == 0 and coeff:
            coeff_num = _num(coeff)
            if coeff_num is not None and coeff_num > 0:
                return _fmt(coeff_num * 100) + "%"
        if kind in {'m', 's'}:
            num = abs(num)
        return _fmt(num)

    # ── DB2 reference-table lookups (Duration / Radius) ──────────────

    def _load_duration_table(self) -> dict[int, int]:
        if self._duration_cache is not None:
            return self._duration_cache
        path = _dump_file('SpellDuration.csv')
        out: dict[int, int] = {}
        if path.exists():
            try:
                with path.open(encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        out[_to_int(row.get('ID'))] = _to_int(row.get('Duration'))
            except Exception:
                out = {}
        self._duration_cache = out
        return out

    def _load_radius_table(self) -> dict[int, float]:
        if self._radius_cache is not None:
            return self._radius_cache
        path = _dump_file('SpellRadius.csv')
        out: dict[int, float] = {}
        if path.exists():
            try:
                with path.open(encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        v = _num(row.get('Radius'))
                        if v is not None:
                            out[_to_int(row.get('ID'))] = v
            except Exception:
                out = {}
        self._radius_cache = out
        return out

    def _load_misc_index(self) -> dict[int, tuple[int, int]]:
        if self._misc_index_cache is not None:
            return self._misc_index_cache
        path = _dump_file('spell_misc_index.csv')
        out: dict[int, tuple[int, int]] = {}
        if path.exists():
            try:
                with path.open(encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        sid = _to_int(row.get('SpellID'))
                        if sid:
                            out[sid] = (_to_int(row.get('DurationIndex')), _to_int(row.get('RangeIndex')))
            except Exception:
                out = {}
        self._misc_index_cache = out
        return out

    def _duration_value(self, spell_id: int, idx: int) -> str:
        """Resolve $d (duration) via SpellMisc → SpellDuration chain.
        Returns seconds as a plain number without unit suffix."""
        spell_id = _to_int(spell_id)
        if spell_id <= 0:
            return ""
        misc = self._load_misc_index()
        entry = misc.get(spell_id)
        if not entry:
            return ""
        duration_idx = entry[0]
        if duration_idx <= 0:
            return ""
        dur_table = self._load_duration_table()
        ms = dur_table.get(duration_idx)
        if ms is None or ms <= 0:
            return ""
        # Convert milliseconds to seconds for display
        sec = ms / 1000
        return _fmt(sec)

    def _radius_value(self, spell_id: int, idx: int) -> str:
        """Resolve $A/$r (radius/aura range) via SpellMisc → SpellRadius chain.
        Returns yards as a plain number without unit suffix."""
        spell_id = _to_int(spell_id)
        if spell_id <= 0:
            return ""
        misc = self._load_misc_index()
        entry = misc.get(spell_id)
        if not entry:
            return ""
        range_idx = entry[1]
        if range_idx <= 0:
            return ""
        rad_table = self._load_radius_table()
        yards = rad_table.get(range_idx)
        if yards is None or yards <= 0:
            return ""
        return _fmt(yards)

    def _resolve_expr(self, expr: str, current_spell_id: int) -> str:
        unresolved = False

        def repl(m):
            nonlocal unresolved
            val = self._resolve_var_match(m, current_spell_id)
            if not val or val.startswith('$'):
                # Variable unresolvable inside expression — use 0 instead
                # of aborting, so partial evaluations still produce a number.
                return '0'
            return val

        expr = re.sub(r"\$abs\s*\(", "abs(", expr or "", flags=re.IGNORECASE)
        replaced = _VAR_RE.sub(repl, expr)
        replaced = replaced.replace(" ", "")
        if not re.fullmatch(r"[0-9+\-*/().absABS]+", replaced):
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
            text = _BARE_COND_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), text)
            text = _BARE_COND_ONE_RE.sub(lambda m: m.group(1) or "", text)
            text = _SWITCH_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), text)
        text = _SPELLNAME_RE.sub(lambda m: self._spell_name(_to_int(m.group(1))) or "", text)
        text = _SPELLDESC_RE.sub("", text)
        text = _SPELLTOOLTIP_RE.sub(lambda m: self.resolve(self._spell_desc(_to_int(m.group(1))), _to_int(m.group(1))), text)
        text = _SPELLAURA_RE.sub(lambda m: self.resolve(self._spell_aura(_to_int(m.group(1))), _to_int(m.group(1))), text)
        text = _SPELLICON_RE.sub("", text)
        text = _EXPR_RE.sub("x", text)
        text = _VAR_RE.sub(_readable_unresolved_var, text)
        text = re.sub(r"\$\?(?=\D|$)", "", text)
        text = re.sub(r"\$proccooldown", "一段时间", text, flags=re.IGNORECASE)
        text = re.sub(r"\$[hH](?![A-Za-z0-9_])", "点", text)
        text = re.sub(r"\$[ef](?![A-Za-z0-9_])", "x", text, flags=re.IGNORECASE)
        text = re.sub(r"\$[nx](?![A-Za-z0-9_])", "x", text, flags=re.IGNORECASE)
        # Some Blizzard client conditionals are stored as ?c3[...][] after the
        # leading "$" was stripped by earlier resolution passes.  Clean both
        # two-branch and one-branch forms so tooltips never expose raw tokens.
        bare_cond_two = re.compile(r"\?(?:!?\$?[acs]\d+)(?:&!?\$?[acs]\d+)*\[([^\[\]]*)\]\[([^\[\]]*)\]", re.IGNORECASE)
        bare_cond_one = re.compile(r"\?(?:!?\$?[acs]\d+)(?:&!?\$?[acs]\d+)*\[([^\[\]]*)\]", re.IGNORECASE)
        prev = None
        while prev != text:
            prev = text
            text = bare_cond_two.sub(lambda m: (m.group(1) or m.group(2) or ""), text)
            text = bare_cond_one.sub(lambda m: m.group(1) or "", text)
        text = _NAMED_RE.sub(lambda m: "x" if (m.group(1) or "").strip() else "", text)
        text = re.sub(r"x\.\d+", "x", text)
        # Blizzard appends display precision suffixes such as `.1` after some
        # numeric expressions (`${...}.1`).  Once the expression has been
        # evaluated, keep the computed number and drop the suffix.
        text = re.sub(r"(\d+(?:\.\d+)?)\.1(?=\D|$)", r"\1", text)
        text = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", text)
        text = text.replace("..", ".")
        return self._cleanup(text)


def _readable_unresolved_var(m: re.Match[str]) -> str:
    kind = (m.group('kind') or '').lower()
    if kind == 'd':
        return '一段时间'
    if kind == 'a':
        return '一定'
    if kind == 'r':
        return '一定'
    if kind == 't':
        return '一段时间'
    if kind == 'o':
        return ''
    if kind == 'u':
        return ''
    if kind == 'i':
        return '多'
    if kind in {'n', 'c', 'h', 'x', 'b'}:
        return 'x' if kind != 'h' else '点'
    return 'x'


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
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == 'abs'
            and len(n.args) == 1
            and not n.keywords
        ):
            return abs(walk(n.args[0]))
        raise ValueError("unsupported expression")

    return float(walk(node))
