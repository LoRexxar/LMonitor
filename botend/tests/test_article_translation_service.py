import json
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.services.article_translation_service import (
    ArticleTranslationService,
    GLMTranslationEngine,
    build_translation_service,
)


class FakeEngine:
    last_error = ""

    def __init__(self, responses=None, available=True):
        self.responses = list(responses or [])
        self.prompts = []
        self._available = available

    def available(self):
        return self._available

    def send_message(self, prompt, *, max_tokens):
        self.prompts.append((prompt, max_tokens))
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return ""


class FakeArticle:
    id = 123
    url = "https://example.com/news"

    def __init__(self):
        self.title = "Test Title"
        self.content = "Paragraph one.\nParagraph two."
        self.title_cn = ""
        self.content_cn = ""
        self.saved_fields = []

    def save(self, update_fields=None):
        self.saved_fields.append(list(update_fields or []))


class ArticleTranslationServiceTests(SimpleTestCase):
    def test_translate_title_strips_quotes(self):
        svc = ArticleTranslationService(engine=FakeEngine(['"中文标题"']), sleep_func=lambda _: None)

        self.assertEqual(svc.translate_title("English title"), "中文标题")

    def test_translate_content_returns_json_pairs(self):
        svc = ArticleTranslationService(
            engine=FakeEngine([json.dumps(["第一段", "第二段"], ensure_ascii=False)]),
            sleep_func=lambda _: None,
        )

        result = json.loads(svc.translate_content("Paragraph one.\nParagraph two."))

        self.assertEqual(
            result,
            [
                {"original": "Paragraph one.", "translated": "第一段"},
                {"original": "Paragraph two.", "translated": "第二段"},
            ],
        )

    def test_translate_article_fields_saves_title_when_content_translation_fails(self):
        article = FakeArticle()
        svc = ArticleTranslationService(
            engine=FakeEngine(["标题已翻译", Exception("content failed")]),
            sleep_func=lambda _: None,
        )

        ok = svc.translate_article_fields(article, logger_prefix="test")

        self.assertTrue(ok)
        self.assertEqual(article.title_cn, "标题已翻译")
        self.assertEqual(article.content_cn, "")
        self.assertEqual(article.saved_fields, [["title_cn"]])

    def test_build_translation_service_supports_injected_engine(self):
        engine = FakeEngine(["标题"])
        svc = build_translation_service(engine=engine, sleep_func=lambda _: None)

        self.assertIs(svc.engine, engine)
        self.assertEqual(svc.translate_title("Title"), "标题")

    @patch("botend.services.article_translation_service.GLMClient")
    def test_default_registry_builds_glm_engine(self, mock_glm_client):
        mock_glm_client.return_value.api_key = "fake"

        svc = build_translation_service(engine_name="glm", sleep_func=lambda _: None)

        self.assertIsInstance(svc.engine, GLMTranslationEngine)
        self.assertTrue(svc.available())
