import json
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.services.article_content_service import (
    blocks_to_plain_text,
    dumps_blocks,
    extract_structured_article,
    loads_blocks,
    translate_blocks,
)
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



class ArticleContentServiceTests(SimpleTestCase):
    def test_extract_structured_article_keeps_sections_lists_and_images(self):
        html = """
        <article>
          <h2>Patch Notes</h2>
          <p>Class changes are live.</p>
          <ul><li>Buff A</li><li>Nerf B</li></ul>
          <blockquote>Developer note</blockquote>
          <img src="/images/a.png" alt="A">
        </article>
        """

        blocks = extract_structured_article(html, base_url="https://example.com/news/1")

        self.assertEqual(blocks[0], {"type": "heading", "text": "Patch Notes", "level": 2})
        self.assertIn({"type": "list_item", "text": "Buff A", "ordered": False}, blocks)
        self.assertIn({"type": "quote", "text": "Developer note"}, blocks)
        self.assertIn({"type": "image", "url": "https://example.com/images/a.png", "alt": "A"}, blocks)
        self.assertIn("Class changes are live.", blocks_to_plain_text(blocks))

    def test_translate_blocks_preserves_non_text_blocks(self):
        blocks = [
            {"type": "heading", "text": "Patch Notes", "level": 2},
            {"type": "paragraph", "text": "Class changes are live."},
            {"type": "image", "url": "https://example.com/a.png", "alt": "A"},
        ]
        pairs = [
            {"original": "Patch Notes", "translated": "补丁说明"},
            {"original": "Class changes are live.", "translated": "职业改动已上线。"},
        ]

        translated = translate_blocks(loads_blocks(dumps_blocks(blocks)), pairs)

        self.assertEqual(translated[0]["text"], "补丁说明")
        self.assertEqual(translated[1]["text"], "职业改动已上线。")
        self.assertEqual(translated[2], blocks[2])
