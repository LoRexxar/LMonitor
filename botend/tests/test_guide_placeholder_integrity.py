"""Offline regression: real cache ORM and production translation entry points."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from botend.guide_models import ClassGuideTranslation
from botend.services.class_guide_service import translate_blocks, protect_guide_text, fingerprint
from botend.services.class_guide_translation import translate_fragment_nodes
from botend.services.wow_news_glossary_service import ProtectedText, WowNewsGlossary


class OfflineService:
    def available(self):
        return False

    @property
    def engine(self):
        raise AssertionError('Provider access forbidden')


class ReplayService:
    """Deterministic local responses, never a provider/model call."""
    def __init__(self, answer):
        self.answer = answer
        self.engine = self

    def available(self):
        return True

    def send_message(self, prompt, **kwargs):
        values = json.loads(prompt.split('\n')[-1])
        return json.dumps([self.answer(value) for value in values], ensure_ascii=False)


class GuidePlaceholderIntegrityTests(TestCase):
    def setUp(self):
        from botend.services.class_guide_glossary import GuideGlossary
        self.glossary = GuideGlossary.from_trusted_pairs([('Brewmaster Monk', '酒仙武僧'), ('Mythic+', '大秘境')])
        self.patch_glossary = patch('botend.services.class_guide_service.build_guide_glossary', return_value=self.glossary)
        self.patch_glossary.start()
        self.addCleanup(self.patch_glossary.stop)

    def test_canonical_counts_and_extra_malformed_tokens(self):
        for token in ('⟦WOWTERM_001⟧', '⟦WOWTERM_1000⟧', '⟦WOWTERM_00001⟧'):
            protected = ProtectedText(token + token, {token: '术语'})
            self.assertTrue(protected.is_intact(token + '中文' + token))
            for text in (token, token * 3, token * 2 + '⟦WOWTERM_99999⟫', token * 2 + '⟪WOWTERM_99999⟧'):
                with self.subTest(token=token, text=text):
                    self.assertFalse(protected.is_intact(text))

    def test_real_cache_2389_is_rejected_and_preserved(self):
        row = json.loads((Path(__file__).parent / 'fixtures/maxroll_placeholder_cache_2389.json').read_text())
        protected = protect_guide_text(row['source'], self.glossary)
        self.assertEqual(protected.text, row['protected'])
        self.assertEqual(fingerprint(['guide-v1', '12.1.5', protected.text, protected.replacements]), row['key'])
        cached = ClassGuideTranslation.objects.create(key=row['key'], source=row['source'], translated=row['cached_translated'])
        out, failed = translate_blocks([{'id':row['block_id'], 'type':'html', 'title':row['source']}], '12.1.5', service=OfflineService())
        self.assertTrue(failed)
        self.assertEqual(out[0]['title'], row['source'])
        cached.refresh_from_db()
        self.assertEqual(cached.translated, row['cached_translated'])

    def test_exact_and_prior_cache_reject_leaks_but_reuse_clean(self):
        source = '<p>Build carefully.</p>'
        key = fingerprint(['guide-v1', '12.1.5', source, {}])
        for cache_key in (key, 'historical-key'):
            for text in ('<p>认真配装。⟦WOWTERM_00002⟫</p>', '<p>认真配装。</p>'):
                with self.subTest(cache_key=cache_key, text=text):
                    ClassGuideTranslation.objects.all().delete()
                    cached = ClassGuideTranslation.objects.create(key=cache_key, source=source, translated=text)
                    out, failed = translate_blocks([{'id':'p','type':'html','html':source}], '12.1.5', service=OfflineService())
                    self.assertEqual(bool(failed), 'WOWTERM' in text)
                    self.assertEqual(out[0]['html'], source if failed else text)
                    cached.refresh_from_db()
                    self.assertEqual(cached.translated, text)

    def test_node_cache_rejects_leaks_but_reuses_clean(self):
        source = 'Build carefully.'
        key = hashlib.sha256(json.dumps(['guide-node-v1', '12.1.5', source, {}], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached = ClassGuideTranslation.objects.create(key=key, source=source, translated='认真配装 ⟪WOWTERM_00001⟧')
        self.assertIsNone(translate_fragment_nodes(source, '12.1.5', self.glossary, OfflineService(), cache_only=True))
        cached.refresh_from_db()
        self.assertIn('WOWTERM', cached.translated)
        cached.translated = '认真配装'; cached.save()
        self.assertEqual(translate_fragment_nodes(source, '12.1.5', self.glossary, OfflineService(), cache_only=True), '认真配装')

    def test_new_block_and_node_responses_reject_extra_malformed(self):
        service = ReplayService(lambda text: text.replace('Build', '配装') + '⟦WOWTERM_99999⟫')
        source = 'Build Mythic+'
        out, failed = translate_blocks([{'id':'p','type':'html','html':source}], '12.1.5', service=service)
        self.assertTrue(failed)
        self.assertEqual(out[0]['html'], source)
        self.assertIsNone(translate_fragment_nodes(source, '12.1.5', self.glossary, service))
        self.assertFalse(ClassGuideTranslation.objects.exists())

    def test_bad_cache_can_retranslate_without_overwriting_evidence(self):
        source = 'Build Mythic+'
        protected = protect_guide_text(source, self.glossary)
        key = fingerprint(['guide-v1', '12.1.5', protected.text, protected.replacements])
        bad = '配装 ⟦WOWTERM_00001⟫'
        cached = ClassGuideTranslation.objects.create(key=key, source=source, translated=bad)
        service = ReplayService(lambda text: text.replace('Build', '配装'))
        out, failed = translate_blocks([{'id':'p','type':'html','html':source}], '12.1.5', service=service)
        self.assertFalse(failed)
        self.assertEqual(out[0]['html'], '配装 大秘境')
        cached.refresh_from_db()
        self.assertEqual(cached.translated, bad)

    def test_restore_residual_is_rejected_in_generated_and_local_paths(self):
        # Fault injection at the restoration boundary, not a mocked acceptance result.
        for source in ('Build Mythic+', 'Mythic+'):
            with self.subTest(source=source), patch.object(self.glossary, 'restore', return_value='中文 ⟦WOWTERM_001⟧'):
                service = ReplayService(lambda text: text.replace('Build', '配装'))
                out, failed = translate_blocks([{'id':'p','type':'html','html':source}], '12.1.5', service=service)
                self.assertTrue(failed)
                self.assertEqual(out[0]['html'], source)
                self.assertIsNone(translate_fragment_nodes(source, '12.1.5', self.glossary, service))
                self.assertFalse(ClassGuideTranslation.objects.exists())
