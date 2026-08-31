"""文章翻译使用的高置信度 WoW 术语保护。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Dict, Iterable, Sequence, Set, Tuple

from botend.constants.hero_talents import HERO_SUBTREE_NAME_ZH
from botend.constants.wow import CLASS_CN, SPEC_CN
from utils.log import logger


_TOKEN_TEMPLATE = "⟦WOWTERM_{:03d}⟧"
_TOKEN_PATTERN = re.compile(r"⟦WOWTERM_\d{3}⟧")
_WORD_BOUNDARY = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"
_NAME_CONNECTORS = {"a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with"}

# 数据库不可用时仍可保护少量稳定地下城名；正式数据优先来自激活的 MDT 版本。
_FALLBACK_DUNGEON_TERMS = (
    ("Murder Row", "密谋小径"),
    ("Den of Nalorakk", "纳洛拉克的洞穴"),
    ("The Blinding Vale", "夺目谷"),
    ("Voidscar Arena", "虚空之痕竞技场"),
    ("Altar of Fangs", "毒牙祭坛"),
    ("Ruby Life Pools", "红玉新生法池"),
    ("Temple of Sethraliss", "塞塔里斯神庙"),
    ("King's Rest", "诸王之眠"),
    ("Algethar Academy", "艾杰斯亚学院"),
    ("Magisters Terrace", "魔导师平台"),
    ("Maisara Caverns", "迈萨拉洞窟"),
    ("Nexus Point Xenas", "节点希纳斯"),
    ("Pit of Saron", "萨隆矿坑"),
    ("Seat of the Triumvirate", "执政团之座"),
    ("Skyreach", "通天峰"),
    ("Windrunner Spire", "风行者之塔"),
)


def _split_pascal_name(value: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(value or "")).strip()


def _contains_english_term(text: str, term: str) -> bool:
    if not text or not term:
        return False
    return bool(re.search(_WORD_BOUNDARY.format(re.escape(term)), text, flags=re.IGNORECASE))


def _extract_name_candidates(text: str, limit: int = 500) -> Set[str]:
    """从已有文章文本提取可能的英文专名，不读取链接也不发起请求。"""
    candidates: Set[str] = set()
    for line in re.split(r"[\r\n.!?;:]+", text or ""):
        words = re.findall(r"[A-Za-z][A-Za-z'’.-]*", line)
        for start, first in enumerate(words):
            if not first[:1].isupper():
                continue
            phrase = []
            for index in range(start, min(len(words), start + 6)):
                word = words[index]
                if index > start and not (word[:1].isupper() or word.lower() in _NAME_CONNECTORS):
                    break
                phrase.append(word)
                if word[:1].isupper():
                    candidates.add(" ".join(phrase))
                    if len(candidates) >= limit:
                        return candidates
    return candidates


@dataclass(frozen=True)
class ProtectedText:
    text: str
    replacements: Dict[str, str]


    def is_intact(self, translated: str) -> bool:
        """检查模型是否完整保留了本次签发的所有占位符。"""
        return Counter(_TOKEN_PATTERN.findall(translated or "")) == Counter(_TOKEN_PATTERN.findall(self.text))


class WowNewsGlossary:
    """保守合并稳定常量、当前快照和文章上下文中的中英术语。"""

    def __init__(
        self,
        terms: Sequence[Tuple[str, str]] = (),
        *,
        trusted_terms: Iterable[str] = (),
    ):
        trusted_keys = {str(term or "").strip().casefold() for term in trusted_terms if str(term or "").strip()}
        candidates: Dict[str, set[str]] = {}
        english_names: Dict[str, str] = {}
        for english, chinese in terms:
            english = (english or "").strip()
            chinese = (chinese or "").strip()
            english_key = english.casefold()
            if self._is_candidate(english, chinese, allow_single_word=english_key in trusted_keys):
                candidates.setdefault(english_key, set()).add(chinese)
                english_names.setdefault(english_key, english)

        self._terms = {
            english_names[english_key]: next(iter(chinese_names))
            for english_key, chinese_names in candidates.items()
            if len(chinese_names) == 1
        }
        self._terms_by_key = {english.casefold(): chinese for english, chinese in self._terms.items()}
        self._trusted_terms = {
            english for english in self._terms if english.casefold() in trusted_keys
        }
        names = sorted(self._terms, key=lambda value: (-len(value), value))
        self._pattern = re.compile(
            "|".join(_WORD_BOUNDARY.format(re.escape(name)) for name in names),
            flags=re.IGNORECASE,
        ) if names else None

    @classmethod
    def empty(cls) -> "WowNewsGlossary":
        return cls()

    @classmethod
    def from_pairs(cls, pairs: Iterable[Tuple[str, str]]) -> "WowNewsGlossary":
        return cls(list(pairs))

    @classmethod
    def from_trusted_pairs(cls, pairs: Iterable[Tuple[str, str]]) -> "WowNewsGlossary":
        pairs = list(pairs)
        return cls(pairs, trusted_terms=[english for english, _chinese in pairs])

    @classmethod
    def from_builtin_terms(cls) -> "WowNewsGlossary":
        pairs = []
        for english, chinese in CLASS_CN.items():
            pairs.extend(((english, chinese), (_split_pascal_name(english), chinese)))
        for english, chinese in SPEC_CN.items():
            pairs.extend(((english, chinese), (_split_pascal_name(english), chinese)))
        pairs.extend(HERO_SUBTREE_NAME_ZH.items())
        pairs.extend(_FALLBACK_DUNGEON_TERMS)
        return cls.from_trusted_pairs(pairs)

    @classmethod
    def from_active_talent_metadata(cls) -> "WowNewsGlossary":
        """只加载激活的正式服天赋版本，避免跨构建混用。"""
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
        except Exception as exc:  # 元数据不可用时仍允许模型继续翻译。
            logger.warning("[WowNewsGlossary] cannot load active talent metadata: %s", str(exc)[:300])
            return cls.empty()

    @classmethod
    def from_active_mythic_dungeon_metadata(cls, source_text: str = "") -> "WowNewsGlossary":
        """加载激活 MDT 版本，并只展开文章已命中地下城的局部实体。"""
        try:
            from botend.models import (
                MythicDungeon,
                MythicDungeonAbility,
                MythicDungeonDataVersion,
                MythicDungeonEnemy,
                MythicDungeonFloor,
                MythicDungeonSpell,
            )

            version = MythicDungeonDataVersion.objects.filter(is_active=True).order_by("-imported_at", "-id").first()
            if not version:
                return cls.from_trusted_pairs(_FALLBACK_DUNGEON_TERMS)

            dungeon_rows = list(
                MythicDungeon.objects.filter(data_version=version, is_active=True)
                .exclude(name="").exclude(name_zh="")
                .values("id", "name", "name_zh")
            )
            pairs = list(_FALLBACK_DUNGEON_TERMS)
            pairs.extend((row["name"], row["name_zh"]) for row in dungeon_rows)
            matched_ids = {
                row["id"]
                for row in dungeon_rows
                if not source_text or _contains_english_term(source_text, row["name"])
            }
            contextual_pairs = []
            if matched_ids:
                contextual_pairs.extend(
                    MythicDungeonFloor.objects.filter(dungeon_id__in=matched_ids, is_active=True)
                    .exclude(name="").exclude(name_zh="")
                    .values_list("name", "name_zh")
                )
                contextual_pairs.extend(
                    MythicDungeonEnemy.objects.filter(dungeon_id__in=matched_ids, is_active=True)
                    .exclude(name="").exclude(name_zh="")
                    .values_list("name", "name_zh")
                )
                contextual_pairs.extend(
                    MythicDungeonAbility.objects.filter(enemy__dungeon_id__in=matched_ids, is_active=True)
                    .exclude(name="").exclude(name_zh="")
                    .values_list("name", "name_zh")
                )
                contextual_pairs.extend(
                    MythicDungeonSpell.objects.filter(
                        data_version=version,
                        is_active=True,
                        ability_links__enemy__dungeon_id__in=matched_ids,
                    )
                    .exclude(name="").exclude(name_zh="")
                    .values_list("name", "name_zh")
                    .distinct()
                )
            pairs.extend(contextual_pairs)
            trusted = [english for english, _chinese in pairs]
            return cls(pairs, trusted_terms=trusted)
        except Exception as exc:  # MDT 数据不可用时保留硬编码地下城兜底。
            logger.warning("[WowNewsGlossary] cannot load active mythic dungeon metadata: %s", str(exc)[:300])
            return cls.from_trusted_pairs(_FALLBACK_DUNGEON_TERMS)

    @classmethod
    def from_current_spell_metadata(cls, source_text: str) -> "WowNewsGlossary":
        """按文章已有文本批量查询当前构建法术名称，不扫描网页或外部接口。"""
        candidates = _extract_name_candidates(source_text)
        if not candidates:
            return cls.empty()
        lowered = source_text.lower()
        branch = "wow"
        if re.search(r"\bptr\b|public test realm", lowered, flags=re.IGNORECASE):
            branch = "wowt"
        elif re.search(r"\bbeta\b", lowered, flags=re.IGNORECASE):
            branch = "wow_beta"

        try:
            from botend.models import WowSpellSnapshot, WowSpellSnapshotState

            state = (
                WowSpellSnapshotState.objects.filter(branch=branch, locale="zhCN")
                .only("snapshot_build")
                .first()
            )
            snapshot_build = (getattr(state, "snapshot_build", "") or "").strip()
            if not snapshot_build:
                return cls.empty()
            pairs = list(
                WowSpellSnapshot.objects.filter(
                    branch=branch,
                    locale="zhCN",
                    snapshot_build=snapshot_build,
                    name__in=sorted(candidates),
                )
                .exclude(name="").exclude(name_zh="")
                .values_list("name", "name_zh")
            )
            combat_context_terms = (
                list(CLASS_CN)
                + [_split_pascal_name(name) for name in CLASS_CN]
                + list(SPEC_CN)
                + [_split_pascal_name(name) for name in SPEC_CN]
            )
            has_combat_context = any(
                _contains_english_term(source_text, term)
                for term in combat_context_terms
            )
            trusted = [english for english, _chinese in pairs if has_combat_context and len(english.split()) == 1]
            return cls(pairs, trusted_terms=trusted)
        except Exception as exc:  # 快照不可用时继续使用常量和天赋术语。
            logger.warning("[WowNewsGlossary] cannot load current spell metadata: %s", str(exc)[:300])
            return cls.empty()

    @classmethod
    def from_current_item_metadata(cls, source_text: str) -> "WowNewsGlossary":
        """按文章已有文本批量查询当前物品快照；单词物品名保持交给模型翻译。"""
        candidates = _extract_name_candidates(source_text)
        if not candidates:
            return cls.empty()
        try:
            from botend.models import WowItemSnapshot

            pairs = (
                WowItemSnapshot.objects.filter(name__in=sorted(candidates))
                .exclude(name="").exclude(name_zh="")
                .values_list("name", "name_zh")
            )
            return cls.from_pairs(pairs)
        except Exception as exc:  # 物品快照缺失不应阻断文章翻译。
            logger.warning("[WowNewsGlossary] cannot load current item metadata: %s", str(exc)[:300])
            return cls.empty()

    @classmethod
    def merged(cls, *glossaries: "WowNewsGlossary") -> "WowNewsGlossary":
        """合并独立来源，并只保留中文候选唯一的英文名称。"""
        pairs = [
            (english, chinese)
            for glossary in glossaries
            for english, chinese in glossary._terms.items()
        ]
        trusted = [
            english
            for glossary in glossaries
            for english in glossary._trusted_terms
        ]
        return cls(pairs, trusted_terms=trusted)

    @classmethod
    def prioritized(cls, *glossaries: "WowNewsGlossary") -> "WowNewsGlossary":
        """按来源优先级合并；较低优先级不得覆盖已确认的稳定术语。"""
        pairs = []
        trusted = []
        seen = set()
        for glossary in glossaries:
            for english, chinese in glossary._terms.items():
                key = english.casefold()
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((english, chinese))
                if english in glossary._trusted_terms:
                    trusted.append(english)
        return cls(pairs, trusted_terms=trusted)

    @staticmethod
    def _is_candidate(english: str, chinese: str, *, allow_single_word: bool = False) -> bool:
        if not english or not chinese or english == chinese:
            return False
        # 单词术语容易与普通英文混淆，只允许经过稳定常量或文章上下文确认的来源。
        if len(english.split()) < 2 and not allow_single_word:
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
            english_key = english.casefold()
            token = term_tokens.get(english_key)
            if token is None:
                token = _TOKEN_TEMPLATE.format(len(term_tokens) + 1)
                term_tokens[english_key] = token
                replacements[token] = self._terms_by_key[english_key]
            return token

        return ProtectedText(text=self._pattern.sub(replace, text), replacements=replacements)

    @staticmethod
    def restore(text: str, replacements: Dict[str, str]) -> str:
        restored = text or ""
        for token, chinese in replacements.items():
            restored = restored.replace(token, chinese)
        return restored
