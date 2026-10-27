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
        self.assertEqual(ClassGuideTranslation.objects.filter(source=source, translated='配装 大秘境').count(), 1)
        with patch.object(service, 'send_message', side_effect=AssertionError('Repeated provider call')):
            again, failed = translate_blocks([{'id':'p','type':'html','html':source}], '12.1.5', service=service)
        self.assertFalse(failed)
        self.assertEqual(again, out)

    def test_bad_node_cache_repair_is_persisted_and_reused(self):
        source = 'Build Mythic+'
        protected = self.glossary.protect(source)
        key = hashlib.sha256(json.dumps(['guide-node-v1', '12.1.5', protected.text, protected.replacements], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached = ClassGuideTranslation.objects.create(key=key, source=source, translated='配装 WOWTERM_001')
        service = ReplayService(lambda text: text.replace('Build', '配装'))
        self.assertEqual(translate_fragment_nodes(source, '12.1.5', self.glossary, service), '配装 大秘境')
        cached.refresh_from_db()
        self.assertEqual(cached.translated, '配装 WOWTERM_001')
        with patch.object(service, 'send_message', side_effect=AssertionError('Repeated provider call')):
            self.assertEqual(translate_fragment_nodes(source, '12.1.5', self.glossary, service), '配装 大秘境')
        self.assertEqual(ClassGuideTranslation.objects.count(), 2)

    def test_all_block_recovery_writes_persist_without_changing_bad_rows(self):
        for path in ('historical', 'local', 'nodes', 'fallback'):
            with self.subTest(path=path):
                ClassGuideTranslation.objects.all().delete()
                source = 'Mythic+' if path == 'local' else '<p>Build Mythic+</p>'
                protected = protect_guide_text(source, self.glossary)
                key = fingerprint(['guide-v1', '12.1.5', protected.text, protected.replacements])
                bad = ClassGuideTranslation.objects.create(key=key, source=source, translated='WOWTERM_001')
                expected = '大秘境' if path == 'local' else '<p>配装 大秘境</p>'
                if path == 'historical':
                    ClassGuideTranslation.objects.create(key='historical', source=source, translated=expected)
                if path == 'nodes':
                    node = self.glossary.protect('Build Mythic+')
                    node_key = hashlib.sha256(json.dumps(['guide-node-v1', '12.1.5', node.text, node.replacements], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    ClassGuideTranslation.objects.create(key=node_key, source='Build Mythic+', translated='配装 大秘境')
                service = ReplayService(lambda text: '' if 'HTML' in text else text.replace('Build', '配装')) if path == 'fallback' else OfflineService()
                blocks = [{'id':'p', 'type':'html', 'html':source}]
                out, failed = translate_blocks(blocks, '12.1.5', service=service)
                self.assertFalse(failed)
                self.assertEqual(out[0]['html'], expected)
                bad.refresh_from_db()
                self.assertEqual(bad.translated, 'WOWTERM_001')
                from botend.services.class_guide_translation_cache import GuideTranslationCache
                cache = GuideTranslationCache(key, source, protected)
                self.assertEqual(ClassGuideTranslation.objects.get(key=cache.repair_key).translated, expected)
                again, failed = translate_blocks(blocks, '12.1.5', service=OfflineService())
                self.assertFalse(failed)
                self.assertEqual(again, out)

    def test_repair_insert_race_validates_winner_and_preserves_evidence(self):
        from botend.services.class_guide_translation_cache import GuideTranslationCache
        source = 'Build Mythic+'
        protected = protect_guide_text(source, self.glossary)
        key = fingerprint(['guide-v1', '12.1.5', protected.text, protected.replacements])
        cache = GuideTranslationCache(key, source, protected)
        bad = ClassGuideTranslation.objects.create(key=key, source=source, translated='WOWTERM_001')
        self.assertIsNone(cache.get())
        original = ClassGuideTranslation.objects.get_or_create
        def interleave(**kwargs):
            if kwargs['key'] == cache.repair_key:
                ClassGuideTranslation.objects.create(key=cache.repair_key, source=source, translated='谨慎配装 大秘境')
            return original(**kwargs)
        with patch.object(ClassGuideTranslation.objects, 'get_or_create', side_effect=interleave):
            self.assertEqual(cache.store('配装 大秘境'), '谨慎配装 大秘境')
        self.assertEqual(cache.get(), '谨慎配装 大秘境')
        self.assertEqual(ClassGuideTranslation.objects.count(), 2)
        bad.refresh_from_db()
        self.assertEqual(bad.translated, 'WOWTERM_001')
        for source_value, translated in ((source, 'WOWTERM_002'), ('different source', '配装 大秘境'), (source, '配装 错误术语')):
            ClassGuideTranslation.objects.filter(key=cache.repair_key).update(source=source_value, translated=translated)
            self.assertIsNone(cache.get())
            with self.assertRaises(ValueError):
                cache.store('配装 大秘境')
            self.assertEqual(ClassGuideTranslation.objects.get(key=cache.repair_key).translated, translated)
        with self.assertRaises(ValueError):
            cache.store('WOWTERM_003')
        ClassGuideTranslation.objects.filter(key=cache.repair_key).update(source=source, translated='配装 大秘境')
        changed_protected = ProtectedText(protected.text, {token: '新术语' for token in protected.replacements})
        changed_key = fingerprint(['guide-v1', '12.1.5', changed_protected.text, changed_protected.replacements])
        changed = GuideTranslationCache(changed_key, source, changed_protected)
        self.assertIsNone(changed.get())
        self.assertNotEqual(cache.repair_key, changed.repair_key)

    def test_failed_repair_keeps_only_original_evidence(self):
        source = 'Build Mythic+'
        for nodes in (False, True):
            with self.subTest(nodes=nodes):
                ClassGuideTranslation.objects.all().delete()
                protected = self.glossary.protect(source) if nodes else protect_guide_text(source, self.glossary)
                identity = ['guide-node-v1' if nodes else 'guide-v1', '12.1.5', protected.text, protected.replacements]
                key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest() if nodes else fingerprint(identity)
                ClassGuideTranslation.objects.create(key=key, source=source, translated='WOWTERM_001')
                before = list(ClassGuideTranslation.objects.values())
                service = ReplayService(lambda text: text + 'WOWTERM_999')
                if nodes:
                    self.assertIsNone(translate_fragment_nodes(source, '12.1.5', self.glossary, service))
                else:
                    _, failed = translate_blocks([{'id':'p', 'type':'html', 'html':source}], '12.1.5', service=service)
                    self.assertTrue(failed)
                self.assertEqual(list(ClassGuideTranslation.objects.values()), before)

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
