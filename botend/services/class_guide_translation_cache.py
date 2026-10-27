"""Append-only repair of rejected guide translations; no schema/version flush."""
import hashlib
import json
import re
from collections import Counter

from botend.guide_models import ClassGuideTranslation
from botend.services.class_guide_content import REF_RE
from botend.services.wow_news_glossary_service import has_wowterm_placeholder


class GuideTranslationCache:
    def __init__(self, key, source, protected, *, markup=True):
        self.key = key
        self.source = source
        self.markup = markup
        self.required_terms = Counter()
        for token, chinese in protected.replacements.items():
            self.required_terms[chinese] += protected.text.count(token)
        # The original key already binds protocol, game version and complete terms.
        # Also bind literal source, which protection can otherwise collapse.
        self.repair_key = hashlib.sha256(json.dumps(
            ['guide-cache-repair-v1', key, source], ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()

    def valid(self, text):
        return (isinstance(text, str) and bool(text.strip())
                and not has_wowterm_placeholder(text)
                and Counter(REF_RE.findall(text)) == Counter(REF_RE.findall(self.source))
                and (not self.markup or re.findall(r'<[^>]+>', text) == re.findall(r'<[^>]+>', self.source))
                and all(text.count(term) >= count for term, count in self.required_terms.items()))

    def _valid_row(self, row):
        return row is not None and row.source == self.source and self.valid(row.translated)

    def get(self):
        for key in (self.key, self.repair_key):
            row = ClassGuideTranslation.objects.filter(key=key).first()
            if self._valid_row(row):
                return row.translated
        return None

    def store(self, text):
        if not self.valid(text):
            raise ValueError('Invalid guide translation cache candidate')
        for key in (self.key, self.repair_key):
            # Unique key + get_or_create handles a concurrent insert. Validate and
            # return its winner, never overwrite evidence or silently lose ours.
            row, _ = ClassGuideTranslation.objects.get_or_create(
                key=key, defaults={'source': self.source, 'translated': text})
            if self._valid_row(row):
                return row.translated
        # A corrupt repair slot is an explicit failure, not a successful uncached
        # translation (which would charge again next time). Preserve both rows.
        raise ValueError('Guide translation repair cache identity/content conflict')
