"""Frozen Maxroll syntax-only headings must not require invented Chinese."""
from django.test import TestCase

from botend.guide_models import ClassGuideTranslation
from botend.services.article_translation_service import ArticleTranslationService, FallbackTranslationEngine
from botend.services.class_guide_service import translate_blocks
from botend.services.class_guide_translation import translate_fragment_nodes
from botend.services.class_guide_translation_cache import GuideTranslationCache
from botend.services.wow_news_glossary_service import WowNewsGlossary


class GuideCursorSyntaxTests(TestCase):
    def setUp(self):
        self.glossary = WowNewsGlossary.from_trusted_pairs([])
        self.service = ArticleTranslationService(
            engine=FallbackTranslationEngine(engines=[]), glossary=self.glossary)

    def test_frozen_cursor_headings_preserved_and_cached_by_real_entry(self):
        for spell_id in (260243, 187698):
            with self.subTest(spell_id=spell_id):
                source = f'<p><strong>@Cursor [[spell:{spell_id}]]</strong><em> </em></p>'
                blocks = [{'id': 'cursor-heading', 'type': 'html', 'html': source}]
                translated, failed = translate_blocks(blocks, '12.1.5', service=self.service)
                self.assertEqual(failed, [])
                self.assertEqual(translated[0]['html'], source)
                row = ClassGuideTranslation.objects.get(source=source)
                self.assertEqual(row.translated, source)
                count = ClassGuideTranslation.objects.count()
                self.assertEqual(translate_blocks(blocks, '12.1.5', service=self.service), (translated, []))
                self.assertEqual(ClassGuideTranslation.objects.count(), count)

    def test_prose_malformed_references_and_placeholder_residue_stay_rejected(self):
        for source in (
            '@Cursor Cast [[spell:260243]] on your target',
            '@Cursor [[spell:invalid]]',
            '@Cursor [[spell:260243]] WOWTERM_001',
            '@Cursor [[spell:260243]] ⟦WOWTERM_001⟧',
            '@Cursor',
        ):
            with self.subTest(source=source):
                self.assertIsNone(translate_fragment_nodes(
                    f'<strong>{source}</strong>', '12.1.5', self.glossary,
                    self.service, cache_only=True))
        source = '@Cursor [[spell:260243]]'
        cache = GuideTranslationCache('syntax-test', source, self.glossary.protect(source), markup=False)
        self.assertFalse(cache.valid('@Cursor [[spell:187698]]'))
        self.assertFalse(cache.valid('@Cursor [[spell:260243]] [[spell:260243]]'))
        self.assertFalse(cache.valid('@Cursor [[spell:260243]] WOWTERM_001'))
