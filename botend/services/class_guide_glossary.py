"""攻略语境术语保护：避免把普通动词机械替换为同名技能。"""
import re

from botend.services.wow_news_glossary_service import WowNewsGlossary, ProtectedText


class GuideGlossary(WowNewsGlossary):
    @staticmethod
    def contextual_word(value):
        return ' ' not in value and value.islower() and value.casefold() not in {'aoe', 'bis', 'mythic+'}

    def contextual_match(self, match):
        value = match[0]
        if self.contextual_word(value):
            return True
        # A capitalized word is not sufficient entity evidence: instructions
        # also capitalize verbs. A following determiner + lowercase word is
        # a direct-object cue ("Heal the tank"). Require an instruction/clause
        # start or auxiliary too: "makes Stagger the best ..." names a skill.
        # Acronyms, multiword names and standalone labels remain protected.
        return bool(re.fullmatch(r'[A-Z][a-z]+', value) and re.search(
            r'(?:^|[.!?;:\n])\s*$|\b(?:to|can|could|should|must|will|would)\s+(?:not\s+)?$',
            match.string[:match.start()], flags=re.IGNORECASE,
        ) and re.match(
            r'\s+(?:the|a|an|your|our|their|these|those)\s+[a-z]+\b',
            match.string[match.end():],
        ))

    def needs_new_translation(self, value):
        # Exact keys already bind protected text + replacements. Historical
        # source-only reuse must not revive a removed skill mapping or the old
        # AoE default: "范围" is a substring of the incorrect "范围伤害".
        return bool(self._pattern and any(
            self.contextual_match(match) or (
                match[0].casefold() == 'aoe' and self._terms_by_key['aoe'] == '范围'
            ) for match in self._pattern.finditer(value)))

    def protect(self, text):
        if not text or not self._pattern:
            return ProtectedText(text=text or '', replacements={})
        replacements, tokens = {}, {}
        def replace(match):
            if self.contextual_match(match):
                return match[0]
            key = match[0].casefold()
            if key not in tokens:
                tokens[key] = '⟦WOWTERM_{:03d}⟧'.format(len(tokens) + 1)
                replacements[tokens[key]] = self._terms_by_key[key]
            return tokens[key]
        return ProtectedText(text=self._pattern.sub(replace, text), replacements=replacements)
