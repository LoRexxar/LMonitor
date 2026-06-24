import json
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.services.article_content_service import (
    article_blocks_match_reference,
    blocks_to_plain_text,
    dumps_blocks,
    extract_structured_article,
    loads_blocks,
    translate_blocks,
)
from botend.services.article_translation_service import (
    ArticleTranslationService,
    FallbackTranslationEngine,
    GLMTranslationEngine,
    build_translation_service,
)


class FakeEngine:
    name = "fake"
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
        self.content_blocks = ""
        self.content_blocks_cn = ""
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

    def test_translate_content_blocks_preserves_structure(self):
        blocks = [
            {"type": "heading", "text": "Classes", "level": 2},
            {"type": "list_item", "text": "Demon Hunter changes.", "ordered": False},
            {"type": "image", "url": "https://example.com/a.png", "alt": "A"},
            {"type": "list_item", "text": "Warrior changes.", "ordered": False},
        ]
        svc = ArticleTranslationService(
            engine=FakeEngine([json.dumps(["职业", "恶魔猎手改动。", "战士改动。"], ensure_ascii=False)]),
            sleep_func=lambda _: None,
        )

        translated = svc.translate_content_blocks(blocks)

        self.assertEqual(len(translated), len(blocks))
        self.assertEqual(translated[0], {"type": "heading", "text": "职业", "level": 2, "original": "Classes"})
        self.assertEqual(translated[1]["text"], "恶魔猎手改动。")
        self.assertEqual(translated[1]["ordered"], False)
        self.assertEqual(translated[2], blocks[2])
        self.assertEqual(translated[3]["text"], "战士改动。")

    def test_translate_article_fields_uses_block_translation_for_content_blocks_cn(self):
        article = FakeArticle()
        article.title_cn = "已有标题"
        article.content = "Classes\nDemon Hunter changes.\nWarrior changes."
        article.content_blocks = dumps_blocks([
            {"type": "heading", "text": "Classes", "level": 2},
            {"type": "list_item", "text": "Demon Hunter changes.", "ordered": False},
            {"type": "image", "url": "https://example.com/a.png", "alt": "A"},
            {"type": "list_item", "text": "Warrior changes.", "ordered": False},
        ])
        svc = ArticleTranslationService(
            engine=FakeEngine([
                json.dumps(["旧字段-职业", "旧字段-恶魔猎手", "旧字段-战士"], ensure_ascii=False),
                json.dumps(["职业", "恶魔猎手改动。", "战士改动。"], ensure_ascii=False),
            ]),
            sleep_func=lambda _: None,
        )

        ok = svc.translate_article_fields(article, logger_prefix="test")
        translated_blocks = loads_blocks(article.content_blocks_cn)

        self.assertTrue(ok)
        self.assertEqual(len(translated_blocks), 4)
        self.assertEqual(translated_blocks[0]["text"], "职业")
        self.assertEqual(translated_blocks[1]["text"], "恶魔猎手改动。")
        self.assertEqual(translated_blocks[2]["type"], "image")
        self.assertEqual(translated_blocks[3]["text"], "战士改动。")
        self.assertEqual(article.saved_fields, [["content_cn", "content_blocks_cn"]])

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

    def test_translate_article_fields_skips_mismatched_content_blocks(self):
        article = FakeArticle()
        article.title_cn = "已有标题"
        article.content_blocks = dumps_blocks([
            {"type": "paragraph", "text": "直播间 1351 人充电 个人资料 预约 收起 bilibili"},
        ])
        svc = ArticleTranslationService(
            engine=FakeEngine([json.dumps(["第一段", "第二段"], ensure_ascii=False)]),
            sleep_func=lambda _: None,
        )

        ok = svc.translate_article_fields(article, logger_prefix="test")

        self.assertTrue(ok)
        self.assertEqual(article.content_blocks_cn, "")
        self.assertEqual(article.saved_fields, [["content_cn"]])

    def test_fallback_engine_uses_codex_when_glm_returns_empty(self):
        glm = FakeEngine([""], available=True)
        glm.name = "glm"
        codex = FakeEngine(["兜底标题"], available=True)
        codex.name = "codex"
        engine = FallbackTranslationEngine([glm, codex])
        svc = ArticleTranslationService(engine=engine, sleep_func=lambda _: None)

        self.assertEqual(svc.translate_title("Title"), "兜底标题")
        self.assertEqual(len(glm.prompts), 1)
        self.assertEqual(len(codex.prompts), 1)

    @patch("botend.services.article_translation_service.GLMClient")
    def test_glm_registry_builds_glm_engine(self, mock_glm_client):
        mock_glm_client.return_value.api_key = "fake"

        svc = build_translation_service(engine_name="glm", sleep_func=lambda _: None)

        self.assertIsInstance(svc.engine, GLMTranslationEngine)
        self.assertTrue(svc.available())

    @patch("botend.services.article_translation_service.CodexTranslationEngine")
    @patch("botend.services.article_translation_service.GLMTranslationEngine")
    def test_default_registry_builds_fallback_engine(self, mock_glm_engine, mock_codex_engine):
        mock_glm_engine.return_value.available.return_value = False
        mock_codex_engine.return_value.available.return_value = True

        svc = build_translation_service(sleep_func=lambda _: None)

        self.assertIsInstance(svc.engine, FallbackTranslationEngine)
        self.assertTrue(svc.available())



class ArticleContentServiceTests(SimpleTestCase):
    def test_extract_structured_article_keeps_source_html(self):
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

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn("<h2>Patch Notes</h2>", blocks[0]["html"])
        self.assertIn('src="https://example.com/images/a.png"', blocks[0]["html"])
        self.assertIn("Class changes are live.", blocks_to_plain_text(blocks))

    def test_extract_structured_article_keeps_nested_list_html(self):
        html = """
        <article>
          <ul>
            <li><strong>DEATH KNIGHT</strong>
              <ul>
                <li><strong>Frost</strong>
                  <ul>
                    <li><em>Developers' notes: tuning changes.</em></li>
                    <li>Pillar of Frost now increases Strength by 20%.</li>
                  </ul>
                </li>
              </ul>
            </li>
          </ul>
        </article>
        """

        blocks = extract_structured_article(html, base_url="https://example.com/news/1")
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn("<ul>", blocks[0]["html"])
        self.assertIn("<strong>DEATH KNIGHT</strong>", blocks[0]["html"])
        self.assertIn("Pillar of Frost now increases Strength by 20%.", blocks_to_plain_text(blocks))

    def test_extract_wowhead_article_keeps_inline_links_in_paragraph(self):
        html = """
        <div id="news-post"><div class="text">
          <div>The <a href="/item=1">Sun Festival's Painted Roc</a>
          is a brand new mount from this year's <a href="/event=341">Midsummer Fire Festival</a>
          which drops from <span><img src="/icon.jpg" alt=""> Frost Lord Ahune</span>.</div>
          <h2>Rewards</h2>
          <div>Requires Level <a href="/spell=1">70</a> Max Stack: 1000</div>
        </div></div>
        """

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn('href="https://www.wowhead.com/item=1"', blocks[0]["html"])
        self.assertIn("Sun Festival's Painted Roc", blocks_to_plain_text(blocks))
        self.assertIn("Requires Level", blocks_to_plain_text(blocks))

    def test_translate_blocks_translates_html_text_without_removing_tags(self):
        blocks = [{"type": "html", "html": '<h2>Patch Notes</h2><p>Class changes are live.</p><img src="https://example.com/a.png"/>'}]
        pairs = [
            {"original": "Patch Notes", "translated": "补丁说明"},
            {"original": "Class changes are live.", "translated": "职业改动已上线。"},
        ]

        translated = translate_blocks(loads_blocks(dumps_blocks(blocks)), pairs)

        self.assertIn("<h2>补丁说明</h2>", translated[0]["html"])
        self.assertIn("<p>职业改动已上线。</p>", translated[0]["html"])
        self.assertIn('src="https://example.com/a.png"', translated[0]["html"])

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

    def test_article_blocks_match_reference_rejects_wrong_page_markers(self):
        blocks = [{"type": "paragraph", "text": "直播间 1351 人充电 个人资料 预约 收起 bilibili"}]
        reference = "Our Unholy Death Knight writer discusses class changes and tier set bonuses."

        self.assertFalse(article_blocks_match_reference(blocks, reference_text=reference))

    def test_article_blocks_match_reference_accepts_shared_article_terms(self):
        blocks = [{"type": "paragraph", "text": "Unholy Death Knight class changes and tier set bonuses are discussed."}]
        reference = "Our Unholy Death Knight writer discusses class changes and tier set bonuses."

        self.assertTrue(article_blocks_match_reference(blocks, reference_text=reference))
