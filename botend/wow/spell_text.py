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


_VAR_RE = re.compile(r"\$(?:(?P<spell>\d+))?(?P<kind>[smAtdoUirnchxb])(?P<idx>\d*)", re.IGNORECASE)
_INLINE_DIV_VAR_RE = re.compile(r"\$/((?P<divisor>\d+));(?:(?P<spell>\d+))?(?P<kind>[smAtdoUirnchxb])(?P<idx>\d*)", re.IGNORECASE)
_LOCALIZATION_RE = re.compile(r"\$[Ll]([^:;]*):([^;]*);")
_LOCALIZATION_LEGACY_RE = re.compile(r"\$[Ll]([A-Za-z\u4e00-\u9fff]+);([A-Za-z\u4e00-\u9fff]+)")
_EXPR_RE = re.compile(r"\$\{([^{}]+)\}(?:\.(\d+))?")
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
_MECHANIC_VALUE_PATTERN = (
    r"(?:"
    r"\$\{[^{}]+\}(?:\.\d+)?"
    r"|\$/\d+;(?:\d+)?[smAtdoUiLrnchxb]\d*"
    r"|\$(?:\d+)?[smbhxc]\d*"
    r"|\$<[^>]+>"
    r")"
)


def _file_cache_key(path: Path) -> tuple[str, int, int]:
    """Return a build-file identity that invalidates if a dump is replaced."""
    resolved = path.expanduser().resolve()
    try:
        stat = resolved.stat()
        return str(resolved), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(resolved), 0, 0


@lru_cache(maxsize=32)
def _cached_dump_spell_text(
    path_value: str,
    _mtime_ns: int,
    _size: int,
    field_name: str,
) -> dict[int, str]:
    path = Path(path_value)
    out: dict[int, str] = {}
    if not path.exists():
        return out
    try:
        with path.open(encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                spell_id = _to_int(row.get('ID'))
                value = (
                    row.get('Name_lang')
                    if field_name == 'name'
                    else row.get('Description_lang') or row.get('AuraDescription_lang')
                )
                value = (value or '').strip()
                if spell_id and value:
                    out[spell_id] = value
    except (OSError, csv.Error, ValueError, TypeError):
        return {}
    return out


@lru_cache(maxsize=16)
def _cached_misc_index(
    index_path_value: str,
    _index_mtime_ns: int,
    _index_size: int,
    misc_path_value: str,
    _misc_mtime_ns: int,
    _misc_size: int,
) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    index_path = Path(index_path_value)
    if index_path.exists():
        try:
            with index_path.open(encoding='utf-8') as handle:
                for row in csv.DictReader(handle):
                    spell_id = _to_int(row.get('SpellID'))
                    if spell_id:
                        out[spell_id] = (
                            _to_int(row.get('DurationIndex')),
                            _to_int(row.get('RangeIndex')),
                        )
        except (OSError, csv.Error, ValueError, TypeError):
            out = {}

    # Exact-build SpellMisc is authoritative; a derived index can be stale.
    misc_path = Path(misc_path_value)
    if misc_path.exists():
        try:
            with misc_path.open(encoding='utf-8-sig') as handle:
                for row in csv.DictReader(handle):
                    spell_id = _to_int(row.get('SpellID'))
                    if spell_id:
                        out[spell_id] = (
                            _to_int(row.get('DurationIndex')),
                            _to_int(row.get('RangeIndex')),
                        )
        except (OSError, csv.Error, ValueError, TypeError):
            pass
    return out


@lru_cache(maxsize=16)
def _cached_dump_effect_index(
    path_value: str,
    _mtime_ns: int,
    _size: int,
) -> dict[int, dict[int, dict[str, str]]]:
    path = Path(path_value)
    index: dict[int, dict[int, dict[str, str]]] = {}
    if not path.exists():
        return index
    try:
        with path.open(newline='', encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                spell_id = _to_int(row.get('SpellID'))
                effect_index = _to_int(row.get('EffectIndex'))
                if not spell_id:
                    continue
                index.setdefault(spell_id, {})[effect_index] = {
                    'base_points': row.get('EffectBasePointsF') or row.get('BasePoints') or '0',
                    'coefficient': (
                        row.get('Coefficient')
                        or row.get('BonusCoefficientFromAP')
                        or row.get('EffectBonusCoefficient')
                        or '0'
                    ),
                    'pvp_multiplier': row.get('PvpMultiplier') or '1',
                    'points_per_resource': row.get('EffectPointsPerResource') or '0',
                }
    except (OSError, csv.Error, ValueError, TypeError):
        return {}
    return index


@dataclass
class SpellTextResolver:
    """Resolve placeholders in spell/talent text using DB snapshots.

    Snapshots are loaded lazily and cached per resolver. The class is safe to
    use from request rendering: database failures simply degrade to cleaned
    placeholder text.
    """

    locale: str = "zhCN"
    branch: str = "wow"
    snapshot_build: str = ""
    dump_dir: str | Path | None = None
    _spell_cache: dict[int, dict[str, str]] = field(default_factory=dict)
    _effect_cache: dict[int, dict[int, dict[str, str]]] = field(default_factory=dict)
    _missing_spells: set[int] = field(default_factory=set)
    _missing_effects: set[int] = field(default_factory=set)
    _duration_cache: dict[int, int] | None = None
    _radius_cache: dict[int, float] | None = None
    _misc_index_cache: dict[int, tuple[int, int]] | None = None
    _max_stacks_cache: dict[int, int] | None = None
    _dump_effect_index_cache: dict[int, dict[int, dict[str, str]]] | None = None
    _dump_spell_name_cache: dict[int, str] | None = None
    _dump_spell_desc_cache: dict[int, str] | None = None

    def resolve(
        self,
        text: str | None,
        spell_id: int | None = None,
        *,
        depth: int = 0,
        spec_index: int | None = None,
        known_spell_ids: set[int] | None = None,
        active_aura_ids: set[int] | None = None,
    ) -> str:
        text = text or ""
        if not text:
            return ""
        if "$" not in text and "?" not in text:
            return self._cleanup_unresolved(text)
        if depth > 3:
            return self._cleanup_unresolved(text)

        sid = _to_int(spell_id)
        try:
            out = text

            out = self._resolve_contextual_conditionals(
                out,
                spec_index,
                known_spell_ids,
                active_aura_ids,
            )
            prev = None
            while prev != out:
                prev = out
                out = _BARE_COND_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), out)
                out = _BARE_COND_ONE_RE.sub(lambda m: m.group(1) or "", out)
                out = _SWITCH_RE.sub(lambda m: (m.group(1) or m.group(2) or ""), out)
            sentence_mark = '。' if self.locale == 'zhCN' else '.'
            out = re.sub(
                r'(?<=[\w%])\s+(?=\$@spellicon\d+)',
                f'{sentence_mark} ',
                out,
                flags=re.IGNORECASE,
            )
            heading_separator = '：' if self.locale == 'zhCN' else ':'
            out = re.sub(
                r'(\$@spellname\d+)\s+(?=\$@spelldesc\d+)',
                rf'\1{heading_separator} ',
                out,
                flags=re.IGNORECASE,
            )
            out = _SPELLNAME_RE.sub(lambda m: self._spell_name(_to_int(m.group(1))) or "", out)
            out = _SPELLDESC_RE.sub(
                lambda m: self.resolve(
                    self._embedded_spell_desc(_to_int(m.group(1))),
                    _to_int(m.group(1)),
                    depth=depth + 1,
                    spec_index=spec_index,
                    known_spell_ids=known_spell_ids,
                    active_aura_ids=active_aura_ids,
                ),
                out,
            )
            out = _SPELLTOOLTIP_RE.sub(
                lambda m: self.resolve(
                    self._embedded_spell_desc(_to_int(m.group(1))),
                    _to_int(m.group(1)),
                    depth=depth + 1,
                    spec_index=spec_index,
                    known_spell_ids=known_spell_ids,
                    active_aura_ids=active_aura_ids,
                ),
                out,
            )
            out = _SPELLAURA_RE.sub(
                lambda m: self.resolve(
                    self._spell_aura(_to_int(m.group(1))),
                    _to_int(m.group(1)),
                    depth=depth + 1,
                    spec_index=spec_index,
                    known_spell_ids=known_spell_ids,
                    active_aura_ids=active_aura_ids,
                ),
                out,
            )
            out = _SPELLICON_RE.sub('', out)
            # Expressions first, so ${$s3/1000}.1 becomes an evaluated value.
            out = _EXPR_RE.sub(lambda m: self._resolve_expr(m.group(1), sid), out)
            out = _INLINE_DIV_VAR_RE.sub(lambda m: self._resolve_inline_div_var_match(m, sid), out)
            out = _VAR_RE.sub(lambda m: self._resolve_var_match(m, sid), out)
            out = self._resolve_localization_switches(out)
            out = _NAMED_RE.sub(lambda m: self._resolve_named(m.group(1), sid), out)
            return self._cleanup_unresolved(out)
        except Exception:
            return self._cleanup_unresolved(text)

    @staticmethod
    def _resolve_localization_switches(text: str) -> str:
        def replace(match):
            prefix = match.string[:match.start()]
            count_match = re.search(r'(-?\d+(?:\.\d+)?)\D*$', prefix)
            count = _num(count_match.group(1)) if count_match else None
            singular, plural = match.group(1), match.group(2)
            return singular if count == 1 else plural

        text = _LOCALIZATION_RE.sub(replace, text)
        text = _LOCALIZATION_LEGACY_RE.sub(replace, text)
        # Some localized rows retain a standalone client localization marker
        # directly before a Chinese noun (for example ``精华$L迸发``).
        return re.sub(r'\$[Ll](?=[\u4e00-\u9fff])', '', text)

    @staticmethod
    def _read_bracket(text: str, start: int) -> tuple[str, int] | None:
        if start >= len(text) or text[start] != '[':
            return None
        depth = 1
        pos = start + 1
        while pos < len(text):
            if text[pos] == '[':
                depth += 1
            elif text[pos] == ']':
                depth -= 1
                if depth == 0:
                    return text[start + 1:pos], pos + 1
            pos += 1
        return None

    @staticmethod
    def _condition_value(
        condition: str,
        spec_index: int | None,
        known_spell_ids: set[int] | None,
        active_aura_ids: set[int] | None,
    ) -> bool | None:
        """Evaluate Blizzard's small boolean condition language.

        Supported operators are ``!``, ``&``, ``|`` and parentheses. Atoms are
        specialization (c), known-spell (s), active-aura (a), or a bare spell ID.
        ``None`` is retained for conditions whose required context is unavailable.
        """
        source = re.sub(r'\s+', '', (condition or '')).replace('$', '')
        pos = 0

        def tri_not(value):
            return None if value is None else not value

        def tri_and(left, right):
            if left is False or right is False:
                return False
            if left is None or right is None:
                return None
            return True

        def tri_or(left, right):
            if left is True or right is True:
                return True
            if left is None or right is None:
                return None
            return False

        def atom_value(token):
            match = re.fullmatch(r'([acs]?)(\d+)', token, re.IGNORECASE)
            if not match:
                return None
            kind, raw_id = match.groups()
            value = int(raw_id)
            kind = kind.lower()
            if kind == 'c':
                return None if spec_index is None else spec_index == value
            if kind == 's':
                return None if known_spell_ids is None else value in known_spell_ids
            if kind == 'a':
                return None if active_aura_ids is None else value in active_aura_ids
            if active_aura_ids is None and known_spell_ids is None:
                return None
            return value in (active_aura_ids or set()) or value in (known_spell_ids or set())

        def parse_primary():
            nonlocal pos
            if pos < len(source) and source[pos] == '(':
                pos += 1
                value = parse_or()
                if pos >= len(source) or source[pos] != ')':
                    return None
                pos += 1
                return value
            match = re.match(r'[acs]?\d+', source[pos:], re.IGNORECASE)
            if not match:
                return None
            pos += len(match.group(0))
            return atom_value(match.group(0))

        def parse_unary():
            nonlocal pos
            if pos < len(source) and source[pos] == '!':
                pos += 1
                return tri_not(parse_unary())
            return parse_primary()

        def parse_and():
            nonlocal pos
            value = parse_unary()
            while pos < len(source) and source[pos] == '&':
                pos += 1
                value = tri_and(value, parse_unary())
            return value

        def parse_or():
            nonlocal pos
            value = parse_and()
            while pos < len(source) and source[pos] == '|':
                pos += 1
                value = tri_or(value, parse_and())
            return value

        result = parse_or()
        return result if pos == len(source) else None

    @classmethod
    def _resolve_contextual_conditionals(
        cls,
        text: str,
        spec_index: int | None,
        known_spell_ids: set[int] | None,
        active_aura_ids: set[int] | None,
    ) -> str:
        """Resolve balanced Blizzard condition branches using talent-row context."""

        def marker_length_at(source: str, pos: int) -> int:
            if source.startswith('$?', pos):
                return 2
            if pos < len(source) and source[pos] == '?' and re.match(
                r'[!()asc\d]', source[pos + 1:pos + 2], re.IGNORECASE
            ):
                return 1
            return 0

        def parse_at(source: str, marker: int):
            marker_len = marker_length_at(source, marker)
            if not marker_len:
                return None
            first_open = source.find('[', marker + marker_len)
            if first_open < 0:
                return None
            condition = source[marker + marker_len:first_open]
            first = cls._read_bracket(source, first_open)
            if not first:
                return None
            true_branch, end = first
            false_branch = ''
            second_start = end
            while second_start < len(source) and source[second_start].isspace():
                second_start += 1
            if second_start < len(source) and source[second_start] == '[':
                second = cls._read_bracket(source, second_start)
                if second:
                    false_branch, end = second
                else:
                    # Some client rows contain an unclosed final false branch. Treat
                    # the rest of the field as that branch instead of leaking it.
                    false_branch = source[second_start + 1:]
                    end = len(source)
            elif marker_length_at(source, second_start):
                # Blizzard encodes else-if as ?cond[A]?cond[B][C]. It is one
                # conditional expression, not two adjacent pieces of text.
                nested = parse_at(source, second_start)
                if nested:
                    false_branch, end = nested
            result = cls._condition_value(
                condition,
                spec_index,
                known_spell_ids,
                active_aura_ids,
            )
            return (false_branch if result is False else true_branch), end

        for _ in range(100):
            dollar_marker = text.find('$?')
            bare_match = re.search(r'(?<!\$)\?(?=[!()asc\d])', text, re.IGNORECASE)
            bare_marker = bare_match.start() if bare_match else -1
            markers = [pos for pos in (dollar_marker, bare_marker) if pos >= 0]
            if not markers:
                return text
            marker = min(markers)
            parsed = parse_at(text, marker)
            if not parsed:
                return text
            replacement, end = parsed
            text = text[:marker] + replacement + text[end:]
        return text

    def resolve_mechanic(
        self,
        text: str | None,
        spell_id: int | None = None,
        *,
        depth: int = 0,
    ) -> str:
        """Render a mechanic-only description without pretending DB2 base values are final.

        Dungeon NPC damage and healing values depend on client hotfixes, difficulty,
        level and runtime scaling.  This path intentionally expands references and
        keeps the mechanic, but degrades unresolved value tokens semantically instead
        of resolving SpellEffect base points or emitting ``x``.
        """

        text = text or ""
        if not text:
            return ""
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
            out = _SPELLNAME_RE.sub(
                lambda m: self._spell_name(_to_int(m.group(1))) or "该技能",
                out,
            )
            if depth <= 3:
                out = _SPELLDESC_RE.sub(
                    lambda m: self.resolve_mechanic(
                        self._spell_desc(_to_int(m.group(1))),
                        _to_int(m.group(1)),
                        depth=depth + 1,
                    ),
                    out,
                )
                out = _SPELLTOOLTIP_RE.sub(
                    lambda m: self.resolve_mechanic(
                        self._spell_desc(_to_int(m.group(1))),
                        _to_int(m.group(1)),
                        depth=depth + 1,
                    ),
                    out,
                )
                out = _SPELLAURA_RE.sub(
                    lambda m: self.resolve_mechanic(
                        self._spell_aura(_to_int(m.group(1))),
                        _to_int(m.group(1)),
                        depth=depth + 1,
                    ),
                    out,
                )
            else:
                out = _SPELLDESC_RE.sub("", out)
                out = _SPELLTOOLTIP_RE.sub("", out)
                out = _SPELLAURA_RE.sub("", out)
            out = _SPELLICON_RE.sub("", out)
            return self._cleanup_mechanic_text(out)
        except Exception:
            return self._cleanup_mechanic_text(text)

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

            snapshot_filters = {
                "branch": self.branch,
                "spell_id": spell_id,
            }
            if self.snapshot_build:
                snapshot_filters["snapshot_build"] = self.snapshot_build
            row = (
                WowSpellSnapshot.objects.filter(locale=self.locale, **snapshot_filters)
                .order_by("-updated_at")
                .first()
            )
            if not row and self.locale != "enUS":
                row = (
                    WowSpellSnapshot.objects.filter(**snapshot_filters)
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
        # A dump selected for the current talent version is exact-build data;
        # never allow an older database snapshot to shadow it.
        dump_name = self._csv_spell_name(spell_id) if self.dump_dir else ''
        if dump_name:
            return dump_name
        snap = self._spell_snapshot(spell_id)
        if self.locale == 'enUS':
            return snap.get("name") or (f"#{spell_id}" if spell_id else "")
        return snap.get("name_zh") or snap.get("name") or (f"#{spell_id}" if spell_id else "")

    def _spell_desc(self, spell_id: int) -> str:
        dump_desc = self._csv_spell_desc(spell_id) if self.dump_dir else ''
        if dump_desc:
            return dump_desc
        snap = self._spell_snapshot(spell_id)
        return snap.get("description") or snap.get("aura_description") or ""

    def _embedded_spell_desc(self, spell_id: int) -> str:
        text = self._spell_desc(spell_id)
        # Resource requirement headers belong to the embedded active spell's
        # action bar tooltip, not to the surrounding passive talent text.
        return re.sub(
            r'^\s*(?:\|[Cc][0-9a-fA-F]{8})?\s*(?:Requires\b|需要)[^\n]*?(?:\|[Rr])?\s*\n{2,}',
            '',
            text,
            count=1,
            flags=re.IGNORECASE,
        )

    def _spell_aura(self, spell_id: int) -> str:
        snap = self._spell_snapshot(spell_id)
        return snap.get("aura_description") or snap.get("description") or self._csv_spell_desc(spell_id) or ""

    def _csv_spell_name(self, spell_id: int) -> str:
        if self._dump_spell_name_cache is None:
            self._dump_spell_name_cache = self._load_dump_spell_text('name')
        return self._dump_spell_name_cache.get(_to_int(spell_id), '')

    def _csv_spell_desc(self, spell_id: int) -> str:
        if self._dump_spell_desc_cache is None:
            self._dump_spell_desc_cache = self._load_dump_spell_text('description')
        return self._dump_spell_desc_cache.get(_to_int(spell_id), '')

    def _load_dump_spell_text(self, field_name: str) -> dict[int, str]:
        locale = self.locale if self.locale in {'zhCN', 'enUS'} else 'zhCN'
        filename = f'SpellName_{locale}.csv' if field_name == 'name' else f'Spell_{locale}.csv'
        path = self._dump_file(filename)
        return _cached_dump_spell_text(*_file_cache_key(path), field_name)

    def _load_dump_effect_index(self) -> dict[int, dict[int, dict[str, str]]]:
        if self._dump_effect_index_cache is None:
            path = self._dump_file('spell_effect_index.csv')
            self._dump_effect_index_cache = _cached_dump_effect_index(*_file_cache_key(path))
        return self._dump_effect_index_cache

    def _effects(self, spell_id: int) -> dict[int, dict[str, str]]:
        spell_id = _to_int(spell_id)
        if spell_id <= 0:
            return {}
        if spell_id in self._effect_cache:
            return self._effect_cache[spell_id]
        exact_rows = self._load_dump_effect_index().get(spell_id)
        if exact_rows:
            self._effect_cache[spell_id] = exact_rows
            return exact_rows
        if spell_id in self._missing_effects:
            return {}
        effects: dict[int, dict[str, str]] = {}
        try:
            from botend.models import WowSpellEffectSnapshot

            filters = {
                "branch": self.branch,
                "locale": self.locale,
                "spell_id": spell_id,
            }
            if self.snapshot_build:
                filters["snapshot_build"] = self.snapshot_build
            for row in WowSpellEffectSnapshot.objects.filter(**filters):
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

    def _resolve_var_match(
        self,
        m: re.Match[str],
        current_spell_id: int,
        *,
        preserve_sign: bool = False,
    ) -> str:
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
        if kind == 'u':
            val = self._max_stacks_value(target_sid)
            if val != "":
                return val
        # Fallback to SpellEffect-based resolution
        val = self._effect_value(
            target_sid,
            idx,
            kind,
            preserve_sign=preserve_sign,
        )
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
            return ""
        divisor = _num(m.group("divisor"))
        num = _num(val)
        if divisor and num is not None:
            return _fmt(num / divisor)
        return val

    def _effect_value(
        self,
        spell_id: int,
        idx: int,
        kind: str,
        *,
        preserve_sign: bool = False,
    ) -> str:
        effects = self._effects(spell_id)
        if not effects:
            return ""
        # Blizzard placeholders are 1-based while DB2 EffectIndex is 0-based:
        # $s1 -> EffectIndex 0, $s2 -> EffectIndex 1.  Older code preferred
        # effects[idx] first, which turned $s1 into the second DB2 effect and
        # produced wrong values (for example 5% became 0%).
        row = effects.get(idx - 1) or effects.get(idx) or {}
        if kind == 'b':
            points_per_resource = _num(row.get('points_per_resource'))
            if points_per_resource is not None and points_per_resource != 0:
                return _fmt(abs(points_per_resource))
            return ''
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
        if num is not None and num == 0:
            # A zero base point commonly means the value is supplied at runtime
            # (for example by attack/spell-power scaling).  Rendering a literal
            # ``0`` is factually wrong; omit the number and preserve the mechanic.
            if coeff:
                coeff_num = _num(coeff)
                if coeff_num is not None and coeff_num > 0:
                    return _fmt(coeff_num * 100) + "%"
            return ""
        # Standalone `$sN`/`$mN` placeholders display a positive magnitude.
        # Arithmetic expressions must retain DB2's sign first: a -25 value in
        # `${-$s2}` evaluates to +25, while -15000 in `${$s2/-1000}` is +15.
        if kind in {'m', 's'} and not preserve_sign:
            num = abs(num)
        return _fmt(num)

    # ── DB2 reference-table lookups (Duration / Radius) ──────────────

    def _load_duration_table(self) -> dict[int, int]:
        if self._duration_cache is not None:
            return self._duration_cache
        path = self._dump_file('SpellDuration.csv')
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
        path = self._dump_file('SpellRadius.csv')
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
        if self._misc_index_cache is None:
            index_path = self._dump_file('spell_misc_index.csv')
            misc_path = self._dump_file('SpellMisc.csv')
            self._misc_index_cache = _cached_misc_index(
                *_file_cache_key(index_path),
                *_file_cache_key(misc_path),
            )
        return self._misc_index_cache

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

    def _max_stacks_value(self, spell_id: int) -> str:
        """Resolve ``$u`` via SpellAuraOptions.CumulativeAura."""
        if self._max_stacks_cache is None:
            values = {}
            path = self._dump_file('SpellAuraOptions.csv')
            if path.exists():
                try:
                    with path.open(encoding='utf-8-sig') as f:
                        for row in csv.DictReader(f):
                            sid = _to_int(row.get('SpellID'))
                            stacks = _to_int(row.get('CumulativeAura'))
                            if sid and stacks > 0:
                                values[sid] = stacks
                except Exception:
                    values = {}
            self._max_stacks_cache = values
        stacks = self._max_stacks_cache.get(_to_int(spell_id), 0)
        return str(stacks) if stacks > 0 else ""

    def _dump_file(self, filename: str) -> Path:
        if self.dump_dir:
            return Path(self.dump_dir) / filename
        return _dump_file(filename)

    def _resolve_expr(self, expr: str, current_spell_id: int) -> str:
        unresolved = False

        def repl(m):
            nonlocal unresolved
            val = self._resolve_var_match(m, current_spell_id, preserve_sign=True)
            if not val or val.startswith('$'):
                unresolved = True
                return '0'
            return val

        expr = re.sub(r"\$abs\s*\(", "abs(", expr or "", flags=re.IGNORECASE)
        replaced = _VAR_RE.sub(repl, expr)
        if unresolved:
            return '${' + expr + '}'
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

    def _cleanup_mechanic_text(self, text: str) -> str:
        text = str(text or "")
        value = _MECHANIC_VALUE_PATTERN

        # Preserve Chinese mechanics while deliberately omitting untrusted values.
        text = re.sub(
            rf"每\s*(?:\$(?:\d+)?[to]\d*|{value})\s*秒",
            "周期性",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"持续\s*\$(?:\d+)?d\d*(?:\s*秒)?",
            "持续一段时间",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\$(?:\d+)?[aArR]\d*\s*码(?:范围)?内",
            "附近",
            text,
        )
        text = re.sub(
            r"\$(?:\d+)?[aArR]\d*\s*码",
            "一定范围",
            text,
        )
        text = re.sub(
            rf"(造成(?:额外的?)?|受到|承受)\s*{value}\s*点\s*"
            r"((?:物理|火焰|冰霜|自然|暗影|神圣|奥术|混沌|宇宙)?伤害)",
            r"\1\2",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"恢复\s*{value}\s*%?\s*(?:的)?(?:最大)?生命值",
            "恢复生命值",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"(吸收)\s*{value}\s*点\s*伤害",
            r"\1伤害",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"(提高|降低|增加|减少)\s*{value}\s*%",
            r"\1",
            text,
            flags=re.IGNORECASE,
        )
        count_words = {
            "个": "多个",
            "名": "多名",
            "次": "多次",
            "层": "多层",
            "枚": "多枚",
        }
        for unit, replacement in count_words.items():
            text = re.sub(
                rf"{value}\s*{unit}",
                replacement,
                text,
                flags=re.IGNORECASE,
            )

        # Small English fallback set, mainly for records without zhCN text.
        text = re.sub(
            rf"\bevery\s+{value}\s+seconds?\b",
            "periodically",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\bfor\s+{value}\s+seconds?\b",
            "for a duration",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\bdeals?\s+{value}\s+((?:physical|fire|frost|nature|shadow|holy|arcane|chaos)?\s*damage)",
            r"deals \1",
            text,
            flags=re.IGNORECASE,
        )

        proc_cooldown = "一段时间" if self.locale == 'zhCN' else "a duration"
        text = re.sub(r"\$proccooldown", proc_cooldown, text, flags=re.IGNORECASE)
        text = _EXPR_RE.sub("", text)
        text = _INLINE_DIV_VAR_RE.sub("", text)
        text = _VAR_RE.sub(lambda m: _mechanic_unresolved_var(m, self.locale), text)
        text = _NAMED_RE.sub("", text)
        text = re.sub(r"\$[ef](?![A-Za-z0-9_])", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\$\?(?=\D|$)", "", text)
        text = text.replace("$", "")

        text = re.sub(r"每(?:隔)?\s*一段时间\s*秒", "周期性", text)
        text = re.sub(r"每(?:隔)?\s*周期性\s*秒?", "周期性", text)
        text = re.sub(r"在\s*(?:周期性|一段时间)\s*秒内", "在一段时间内", text)
        text = re.sub(r"持续\s*一段时间\s*秒", "持续一段时间", text)
        text = re.sub(r"一定范围\s*码(?:范围)?内", "附近", text)
        text = re.sub(r"一定范围\s*码", "一定范围", text)
        text = re.sub(r"一定范围\s*范围内", "一定范围内", text)
        text = re.sub(r"(造成|受到|承受)\s*点\s*", r"\1", text)
        text = re.sub(r"受到的点(?=伤害|治疗量|治疗效果)", "受到的", text)
        text = re.sub(r"接下来的点(?=伤害|治疗量|治疗效果)", "接下来的", text)
        text = re.sub(r"吸取点(?=生命)", "吸取", text)
        text = re.sub(r"(提高|降低|增加|减少)\s*%", r"\1", text)
        text = re.sub(r"有\s*%\s*几率", "有一定几率", text)
        text = re.sub(r"(?<![A-Za-z])x(?![A-Za-z])", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+([，。；、,.!?])", r"\1", text)
        text = re.sub(r"([，。；、,.!?])\1+", r"\1", text)
        text = re.sub(r"\s+", " ", text)
        text = text.replace("..", ".")
        text = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", text, flags=re.IGNORECASE)
        return text.strip()

    def _cleanup_unresolved(self, text: str) -> str:
        text = text or ""
        # Colored requirement headers are separate paragraphs in Blizzard text.
        # Preserve that boundary before whitespace and color codes are removed.
        sentence_mark = '。' if self.locale == 'zhCN' else '.'
        text = re.sub(
            r'(?<![。.!?])(\|[Rr])\s*\n{2,}',
            rf'\1{sentence_mark} ',
            text,
        )
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
        text = _EXPR_RE.sub("", text)
        text = _VAR_RE.sub(lambda m: _readable_unresolved_var(m, self.locale), text)
        text = re.sub(r"(造成|受到|承受)\s*最多\s*点\s*", r"\1", text)
        text = re.sub(r"(造成|受到|承受)\s*点\s*", r"\1", text)
        text = re.sub(r"(需要|产生|获得|消耗|恢复)\s*点(?=[\u4e00-\u9fff])", r"\1", text)
        text = re.sub(r"达到\s*点(?=时)", "达到上限", text)
        text = re.sub(r"在\s*(\d+(?:\.\d+)?)\s*后", r"在\1秒后", text)
        text = re.sub(r"(\d+(?:\.\d+)?秒内)\s*点(?=伤害|治疗)", r"\1造成", text)
        text = re.sub(r"恢复\s*个(?=[\u4e00-\u9fff])", "恢复", text)
        text = text.replace('一定码内', '一定范围内')
        text = re.sub(r"在\s*(\d+(?:\.\d+)?)\s*内", r"在\1秒内", text)
        text = re.sub(r"(接下来的?)\s*(\d+(?:\.\d+)?)\s*内", r"\1\2秒内", text)
        text = re.sub(r"持续\s*(\d+(?:\.\d+)?)\s*(?=[。；，,]|$)", r"持续\1秒", text)
        text = re.sub(r"有\s*%\s*(?:的)?几率", "有一定几率", text)
        text = re.sub(r"([\u4e00-\u9fff]{0,12})提高\s*%", lambda m: f"{m.group(1)}会有所提高", text)
        text = re.sub(r"([\u4e00-\u9fff]{0,12})降低\s*%", lambda m: f"{m.group(1)}会有所降低", text)
        text = re.sub(r"\$L([^:;]*):([^;]*);", lambda m: m.group(1) or m.group(2), text, flags=re.IGNORECASE)
        text = text.replace('[', '').replace(']', '')
        text = re.sub(r"\$\?(?=\D|$)", "", text)
        proc_cooldown = "一段时间" if self.locale == 'zhCN' else "a duration"
        text = re.sub(r"\$proccooldown", proc_cooldown, text, flags=re.IGNORECASE)
        h_fallback = "点" if self.locale == 'zhCN' else "points"
        text = re.sub(r"\$[hH](?![A-Za-z0-9_])", h_fallback, text)
        text = re.sub(r"\$[ef](?![A-Za-z0-9_])", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\$[nx](?![A-Za-z0-9_])", "", text, flags=re.IGNORECASE)
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
        text = _NAMED_RE.sub("", text)
        text = re.sub(r"x\.\d+", "", text)
        # Blizzard appends display precision suffixes such as `.1` after some
        # numeric expressions (`${...}.1`).  Once the expression has been
        # evaluated, keep the computed number and drop the suffix.
        text = re.sub(r"(\d+(?:\.\d+)?)\.1(?=\D|$)", r"\1", text)
        text = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", text, flags=re.IGNORECASE)
        if self.locale == 'zhCN':
            # Runtime-only values must degrade to a complete sentence rather
            # than leaving dangling Chinese units such as “跳跃码”/“额外的%”.
            text = re.sub(r"向前跳跃\s*码", "向前跳跃", text)
            text = re.sub(r"提高额外的\s*%", "进一步提高", text)
            text = re.sub(r"投掷\s*枚(?=[\u4e00-\u9fff])", "投掷", text)
            text = re.sub(r"共?造成\s*次伤害", "造成伤害", text)
            text = re.sub(
                r"((?:眩晕|昏迷|定身|无法移动))\s*(\d+(?:\.\d+)?)(?=[。；，,.!?])",
                r"\1\2秒",
                text,
            )
        else:
            text = re.sub(r"(?<![\d.])\s+yds?\b", "", text, flags=re.IGNORECASE)
            text = re.sub(
                r"by\s+an\s+additional\s*%",
                "further",
                text,
                flags=re.IGNORECASE,
            )
            # A bare number after “for” at sentence end is a resolved duration
            # whose source text omitted the unit.
            text = re.sub(
                r"\bfor\s+(\d+(?:\.\d+)?)(?=[.,;!?])",
                r"for \1 sec",
                text,
                flags=re.IGNORECASE,
            )
        text = text.replace("..", ".")
        return self._cleanup(text)


def _readable_unresolved_var(m: re.Match[str], locale: str = 'zhCN') -> str:
    kind = (m.group('kind') or '').lower()
    if locale == 'enUS':
        if kind in {'d', 't'}:
            return 'a duration'
        if kind in {'a', 'r'}:
            return 'a range'
        if kind in {'u', 'i', 'n'}:
            return 'multiple'
        if kind == 'h':
            return 'points'
        return ''
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
    if kind == 'h':
        return '点'
    # Unknown scalar values are omitted rather than fabricated as ``x`` or
    # ``0``.  The surrounding cleanup keeps the mechanic readable.
    return ''


def _mechanic_unresolved_var(m: re.Match[str], locale: str = 'zhCN') -> str:
    kind = (m.group("kind") or "").lower()
    if locale == 'enUS':
        if kind == 'd':
            return 'a duration'
        if kind in {'a', 'r'}:
            return 'a range'
        if kind in {'t', 'o'}:
            return 'periodically'
        if kind in {'u', 'i', 'n'}:
            return 'multiple'
        return ''
    if kind == "d":
        return "一段时间"
    if kind in {"a", "r"}:
        return "一定范围"
    if kind in {"t", "o"}:
        return "周期性"
    if kind in {"u", "i", "n"}:
        return "多"
    return ""


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
