"""High-confidence WoW terminology protection for article translation.

The translation model receives stable placeholders for recognized game terms.  After
translation, placeholders are deterministically restored to the localized name from
our versioned talent metadata.  This keeps official names consistent without
rewriting links, HTML attributes, or the rest of an article's prose.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, Iterable, Sequence, Tuple

from utils.log import logger


_TOKEN_TEMPLATE = "⟦WOWTERM_{:03d}⟧"
_WORD_BOUNDARY = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"


@dataclass(frozen=True)
class ProtectedText:
    text: str
    replacements: Dict[str, str]


    def is_intact(self, translated: str) -> bool:
        """Return whether all protected terms survived model output unchanged."""
        if not self.replacements:
            return True
        return all(
            (translated or "").count(token) == self.text.count(token)
            for token in self.replacements
        )


class WowNewsGlossary:
    """A conservative English-to-Chinese terminology map for game news.

    Only multi-word English names with exactly one Chinese candidate are eligible.
    One-word terms are common English vocabulary, and ambiguous English names need
    a spell ID or article-specific context before they can safely be protected.
    """

    def __init__(self, terms: Sequence[Tuple[str, str]] = ()):  # pairs aid tests/injection
        candidates: Dict[str, set[str]] = {}
        for english, chinese in terms:
            english = (english or "").strip()
            chinese = (chinese or "").strip()
            if self._is_candidate(english, chinese):
                candidates.setdefault(english, set()).add(chinese)

        self._terms = {
            english: next(iter(chinese_names))
            for english, chinese_names in candidates.items()
            if len(chinese_names) == 1
        }
        names = sorted(self._terms, key=lambda value: (-len(value), value))
        self._pattern = re.compile("|".join(_WORD_BOUNDARY.format(re.escape(name)) for name in names)) if names else None

    @classmethod
    def empty(cls) -> "WowNewsGlossary":
        return cls()

    @classmethod
    def from_pairs(cls, pairs: Iterable[Tuple[str, str]]) -> "WowNewsGlossary":
        return cls(list(pairs))

    @classmethod
    def from_active_talent_metadata(cls) -> "WowNewsGlossary":
        """Load only the active Retail talent version, never a cross-build snapshot.

        ``WowSpellSnapshot`` intentionally retains mixed historical branch/build
        data in current deployments.  Talent metadata has an explicit version
        relationship, so it is the safe source for this first glossary release.
        """
        try:
            from botend.models import WowTalentNodeMetadata, WowTalentVersion

            version = (
                WowTalentVersion.objects.filter(
                    is_active=True,
                    is_default_player_tree=True,
                    branch="retail",
                )
                .order_by("-id")
                .first()
            )
            if not version:
                return cls.empty()
            pairs = WowTalentNodeMetadata.objects.filter(
                talent_version=version,
            ).exclude(
                tree_type="hero_anchor",
            ).exclude(
                name="",
            ).exclude(
                name_zh="",
            ).values_list("name", "name_zh")
            return cls.from_pairs(pairs)
        except Exception as exc:  # Translation must remain available without metadata.
            logger.warning("[WowNewsGlossary] cannot load active talent metadata: %s", str(exc)[:300])
            return cls.empty()

    @staticmethod
    def _is_candidate(english: str, chinese: str) -> bool:
        if not english or not chinese or english == chinese:
            return False
        # Single words (e.g. Sentinel, Flurry) are too likely to occur as prose.
        if len(english.split()) < 2:
            return False
        return bool(re.search(r"[A-Za-z]", english))

    @property
    def term_count(self) -> int:
        return len(self._terms)

    def protect(self, text: str) -> ProtectedText:
        text = text or ""
        if not text or not self._pattern:
            return ProtectedText(text=text, replacements={})

        replacements: Dict[str, str] = {}
        term_tokens: Dict[str, str] = {}

        def replace(match: re.Match) -> str:
            english = match.group(0)
            token = term_tokens.get(english)
            if token is None:
                token = _TOKEN_TEMPLATE.format(len(term_tokens) + 1)
                term_tokens[english] = token
                replacements[token] = self._terms[english]
            return token

        return ProtectedText(text=self._pattern.sub(replace, text), replacements=replacements)

    @staticmethod
    def restore(text: str, replacements: Dict[str, str]) -> str:
        restored = text or ""
        for token, chinese in replacements.items():
            restored = restored.replace(token, chinese)
        return restored
