"""攻略语境术语保护：避免把普通小写动词机械替换为同名技能。"""
from botend.services.wow_news_glossary_service import WowNewsGlossary, ProtectedText


class GuideGlossary(WowNewsGlossary):
    @staticmethod
    def contextual_word(value):
        return ' ' not in value and value.islower() and value.casefold() not in {'aoe', 'bis', 'mythic+'}

    def needs_new_translation(self, value):
        return bool(self._pattern and any(self.contextual_word(match[0]) for match in self._pattern.finditer(value)))

    def protect(self, text):
        if not text or not self._pattern:
            return ProtectedText(text=text or '', replacements={})
        replacements, tokens = {}, {}
        def replace(match):
            if self.contextual_word(match[0]):
                return match[0]
            key = match[0].casefold()
            if key not in tokens:
                tokens[key] = '⟦WOWTERM_{:03d}⟧'.format(len(tokens) + 1)
                replacements[tokens[key]] = self._terms_by_key[key]
            return tokens[key]
        return ProtectedText(text=self._pattern.sub(replace, text), replacements=replacements)
