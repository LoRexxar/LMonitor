import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from bs4 import BeautifulSoup

from botend.services.article_content_service import (
    article_blocks_match_reference,
    blocks_to_plain_text,
    dumps_blocks,
    extract_structured_article,
    loads_blocks,
    normalize_blizzard_tracker_html_fragment,
    normalize_wowhead_html_fragment,
    translate_blocks,
)
from botend.services.article_image_service import _fetch_image_response, upload_article_html_images, upload_article_images_in_blocks
from botend.services.article_translation_service import (
    ArticleTranslationService,
    FallbackTranslationEngine,
    GLMTranslationEngine,
    build_translation_service,
)
from botend.services.wow_news_glossary_service import WowNewsGlossary
from botend.models import (
    MythicDungeon,
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonEnemy,
    MythicDungeonFloor,
    WowItemSnapshot,
    WowSpellSnapshot,
    WowSpellSnapshotState,
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


class WowNewsGlossaryBuiltinTests(SimpleTestCase):
    def test_builtin_terms_cover_class_spec_hero_tree_and_dungeon_names(self):
        glossary = WowNewsGlossary.from_builtin_terms()

        protected = glossary.protect(
            "Death Knight Frost and Sentinel enter Ruby Life Pools."
        )
        restored = glossary.restore(protected.text, protected.replacements)

        self.assertEqual(restored, "死亡骑士 冰霜 and 哨兵 enter 红玉新生法池.")

    def test_builtin_terms_match_source_names_case_insensitively(self):
        glossary = WowNewsGlossary.from_builtin_terms()

        protected = glossary.protect("death knight enters ruby life pools.")
        restored = glossary.restore(protected.text, protected.replacements)

        self.assertEqual(restored, "死亡骑士 enters 红玉新生法池.")


class ArticleTranslationServiceTests(SimpleTestCase):
    def setUp(self):
        self.glossary_loaders = [
            patch.object(WowNewsGlossary, method, return_value=WowNewsGlossary.empty())
            for method in (
                "from_builtin_terms",
                "from_active_talent_metadata",
                "from_active_mythic_dungeon_metadata",
                "from_current_spell_metadata",
                "from_current_item_metadata",
            )
        ]
        for loader in self.glossary_loaders:
            loader.start()
            self.addCleanup(loader.stop)

    def test_translate_title_strips_quotes(self):
        svc = ArticleTranslationService(engine=FakeEngine(['"中文标题"']), sleep_func=lambda _: None)

        self.assertEqual(svc.translate_title("English title"), "中文标题")

    def test_translate_title_forces_wowhead_live_label_to_retail(self):
        engine = FakeEngine(["直播版本更新"])
        svc = ArticleTranslationService(engine=engine, sleep_func=lambda _: None)

        self.assertEqual(svc.translate_title("[Live] Version Update"), "[正式服]直播版本更新")
        self.assertNotIn("[Live]", engine.prompts[0][0])

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

    def test_content_prompt_requires_translating_non_placeholder_english(self):
        svc = ArticleTranslationService(engine=FakeEngine(), sleep_func=lambda _: None)

        self.assertIn("不得保留英文", svc._translation_prompt(["Developers’ notes"]))

    def test_glossary_protects_unique_multiword_terms_and_restores_official_chinese(self):
        glossary = WowNewsGlossary.from_pairs([
            ("Arcane Surge", "奥术涌动"),
            ("Touch of the Magi", "织法者之触"),
            ("Sentinel", "哨兵"),
            ("Flurry", "冰风暴"),
            ("Flurry", "乱舞"),
        ])

        protected = glossary.protect("Arcane Surge buffs Touch of the Magi. Sentinel uses Flurry.")

        self.assertEqual(
            protected.text,
            "⟦WOWTERM_001⟧ buffs ⟦WOWTERM_002⟧. Sentinel uses Flurry.",
        )
        self.assertEqual(
            glossary.restore("⟦WOWTERM_001⟧强化了⟦WOWTERM_002⟧。", protected.replacements),
            "奥术涌动强化了织法者之触。",
        )

    def test_translate_title_and_html_blocks_restore_glossary_terms(self):
        glossary = WowNewsGlossary.from_pairs([
            ("Arcane Surge", "奥术涌动"),
            ("Touch of the Magi", "织法者之触"),
        ])
        svc = ArticleTranslationService(
            engine=FakeEngine([
                "⟦WOWTERM_001⟧更新",
                json.dumps(["⟦WOWTERM_001⟧", "强化⟦WOWTERM_001⟧。"], ensure_ascii=False),
            ]),
            glossary=glossary,
            sleep_func=lambda _: None,
        )

        title = svc.translate_title("Arcane Surge update")
        translated = svc.translate_content_blocks([
            {"type": "html", "html": '<p><a href="/spell=321">Arcane Surge</a> buffs Touch of the Magi.</p>'},
        ])

        self.assertEqual(title, "奥术涌动更新")
        self.assertIn('href="/spell=321"', translated[0]["html"])
        self.assertIn("奥术涌动", translated[0]["html"])
        self.assertIn("织法者之触", translated[0]["html"])
        self.assertNotIn("WOWTERM", translated[0]["html"])

    def test_title_retries_when_engine_damages_a_protected_term(self):
        glossary = WowNewsGlossary.from_pairs([("Arcane Surge", "奥术涌动")])
        engine = FakeEngine(["奥术涌动更新", "⟦WOWTERM_001⟧更新"])
        svc = ArticleTranslationService(engine=engine, glossary=glossary, sleep_func=lambda _: None)

        self.assertEqual(svc.translate_title("Arcane Surge update"), "奥术涌动更新")
        self.assertEqual(len(engine.prompts), 2)

    def test_title_retries_when_engine_invents_a_glossary_token(self):
        engine = FakeEngine(["午夜：⟦WOWTERM_001⟧ 开发说明", "午夜开发说明"])
        svc = ArticleTranslationService(engine=engine, sleep_func=lambda _: None)

        self.assertEqual(svc.translate_title("Midnight PTR Development Notes"), "午夜开发说明")
        self.assertEqual(len(engine.prompts), 2)

    def test_translate_content_blocks_uses_unique_indexes_for_html_and_text(self):
        glossary = WowNewsGlossary.from_pairs([
            ("Arcane Surge", "奥术涌动"),
            ("Touch of the Magi", "织法者之触"),
            ("Aimed Shot", "瞄准射击"),
        ])
        svc = ArticleTranslationService(
            engine=FakeEngine([
                json.dumps([
                    "⟦WOWTERM_001⟧", "⟦WOWTERM_001⟧", "⟦WOWTERM_001⟧",
                ], ensure_ascii=False),
            ]),
            glossary=glossary,
            sleep_func=lambda _: None,
        )
        blocks = [
            {"type": "html", "html": "<p>Arcane Surge</p><p>Touch of the Magi</p>"},
            {"type": "paragraph", "text": "Aimed Shot"},
        ]

        translated = svc.translate_content_blocks(blocks)

        self.assertIn("奥术涌动", translated[0]["html"])
        self.assertIn("织法者之触", translated[0]["html"])
        self.assertEqual(translated[1]["text"], "瞄准射击")

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

    def test_midnight_dungeon_article_restores_active_mdt_names(self):
        article = FakeArticle()
        article.title = "Midnight Season 2 Dungeon Test"
        article.title_cn = "已有标题"
        article.content = "Ruby Life Pools: Melidrussa Chillworn and Kokia Blazehoof."
        article.content_blocks = dumps_blocks([
            {"type": "paragraph", "text": "Ruby Life Pools: Melidrussa Chillworn and Kokia Blazehoof."},
        ])
        mdt_glossary = WowNewsGlossary.from_pairs([
            ("Ruby Life Pools", "红玉新生法池"),
            ("Melidrussa Chillworn", "梅莉杜莎·寒妆"),
            ("Kokia Blazehoof", "柯姬雅·焰蹄"),
        ])
        svc = ArticleTranslationService(
            engine=FakeEngine([
                json.dumps(["⟦WOWTERM_001⟧：⟦WOWTERM_002⟧与⟦WOWTERM_003⟧。"], ensure_ascii=False),
                json.dumps(["⟦WOWTERM_001⟧：⟦WOWTERM_002⟧与⟦WOWTERM_003⟧。"], ensure_ascii=False),
            ]),
            glossary=WowNewsGlossary.empty(),
            sleep_func=lambda _: None,
        )

        with patch.object(WowNewsGlossary, "from_active_mythic_dungeon_metadata", return_value=mdt_glossary):
            self.assertTrue(svc.translate_article_fields(article, logger_prefix="test"))

        self.assertIn("红玉新生法池：梅莉杜莎·寒妆与柯姬雅·焰蹄。", article.content_cn)
        self.assertEqual(loads_blocks(article.content_blocks_cn)[0]["text"], "红玉新生法池：梅莉杜莎·寒妆与柯姬雅·焰蹄。")

    def test_dungeon_glossary_no_longer_requires_midnight_literal(self):
        article = FakeArticle()
        article.title = "Ruby Life Pools Dungeon Test"
        article.content = "Melidrussa Chillworn uses Frost Overload."
        mdt_glossary = WowNewsGlossary.from_trusted_pairs([
            ("Ruby Life Pools", "红玉新生法池"),
            ("Melidrussa Chillworn", "梅莉杜莎·寒妆"),
            ("Frost Overload", "冰霜过载"),
        ])
        svc = ArticleTranslationService(
            engine=FakeEngine(),
            glossary=WowNewsGlossary.empty(),
            sleep_func=lambda _: None,
        )

        with patch.object(WowNewsGlossary, "from_active_mythic_dungeon_metadata", return_value=mdt_glossary) as loader:
            glossary = svc._glossary_for_article(article)

        protected = glossary.protect("Ruby Life Pools: Melidrussa Chillworn uses Frost Overload.")
        restored = glossary.restore(protected.text, protected.replacements)
        self.assertEqual(restored, "红玉新生法池: 梅莉杜莎·寒妆 uses 冰霜过载.")
        loader.assert_called_once_with(source_text="Ruby Life Pools Dungeon Test Melidrussa Chillworn uses Frost Overload.")

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


class WowNewsGlossaryDatabaseTests(TestCase):
    def test_current_spell_metadata_uses_only_active_snapshot_build(self):
        WowSpellSnapshotState.objects.create(
            branch="wow",
            locale="zhCN",
            snapshot_build="12.1.0-current",
        )
        WowSpellSnapshot.objects.create(
            branch="wow",
            locale="zhCN",
            spell_id=1001,
            name="Arcane Surge",
            name_zh="奥术涌动",
            snapshot_build="12.1.0-current",
        )
        WowSpellSnapshot.objects.create(
            branch="wow",
            locale="zhCN",
            spell_id=1002,
            name="Arcane Barrage",
            name_zh="奥术弹幕",
            snapshot_build="12.0.0-stale",
        )
        WowSpellSnapshot.objects.create(
            branch="wow",
            locale="zhCN",
            spell_id=1003,
            name="Blink",
            name_zh="闪现术",
            snapshot_build="12.1.0-current",
        )

        glossary = WowNewsGlossary.from_current_spell_metadata(
            "Arcane changes: Arcane Surge, Arcane Barrage, and Blink."
        )
        protected = glossary.protect("Arcane Surge, Arcane Barrage, and Blink.")
        restored = glossary.restore(protected.text, protected.replacements)

        self.assertEqual(restored, "奥术涌动, Arcane Barrage, and 闪现术.")

    def test_single_word_spell_requires_class_context(self):
        WowSpellSnapshotState.objects.create(
            branch="wow",
            locale="zhCN",
            snapshot_build="12.1.0-current",
        )
        WowSpellSnapshot.objects.create(
            branch="wow",
            locale="zhCN",
            spell_id=1004,
            name="Blink",
            name_zh="闪现术",
            snapshot_build="12.1.0-current",
        )

        glossary = WowNewsGlossary.from_current_spell_metadata(
            "Movement updates include Blink."
        )

        self.assertEqual(glossary.protect("Blink").text, "Blink")

    def test_dungeon_entities_are_scoped_to_dungeon_mentioned_in_article(self):
        version = MythicDungeonDataVersion.objects.create(
            key="glossary-test",
            label="术语测试",
            is_active=True,
        )
        matched_dungeon = MythicDungeon.objects.create(
            data_version=version,
            key="test-depths",
            name="Test Depths",
            name_zh="测试深渊",
        )
        matched_enemy = MythicDungeonEnemy.objects.create(
            dungeon=matched_dungeon,
            key="stoneguard",
            name="Stoneguard",
            name_zh="石卫",
        )
        MythicDungeonFloor.objects.create(
            dungeon=matched_dungeon,
            key="lower-depths",
            floor_index=1,
            name="Lower Depths",
            name_zh="深渊下层",
        )
        MythicDungeonAbility.objects.create(
            enemy=matched_enemy,
            spell_id=2001,
            name="Crushing Blow",
            name_zh="碾压猛击",
        )
        other_dungeon = MythicDungeon.objects.create(
            data_version=version,
            key="other-halls",
            name="Other Halls",
            name_zh="其他大厅",
        )
        MythicDungeonEnemy.objects.create(
            dungeon=other_dungeon,
            key="watcher",
            name="Watcher",
            name_zh="监视者",
        )

        glossary = WowNewsGlossary.from_active_mythic_dungeon_metadata(
            source_text="Test Depths: Stoneguard uses Crushing Blow in Lower Depths. Watcher waits."
        )
        protected = glossary.protect(
            "Test Depths: Stoneguard uses Crushing Blow in Lower Depths. Watcher waits."
        )
        restored = glossary.restore(protected.text, protected.replacements)

        self.assertEqual(restored, "测试深渊: 石卫 uses 碾压猛击 in 深渊下层. Watcher waits.")

    def test_current_item_metadata_reuses_multiword_item_snapshot(self):
        WowItemSnapshot.objects.create(
            item_id=3001,
            name="Crown of the Cosmos",
            name_zh="宇宙之冠",
        )

        glossary = WowNewsGlossary.from_current_item_metadata(
            "Crown of the Cosmos receives a new effect."
        )
        protected = glossary.protect("Crown of the Cosmos receives a new effect.")
        restored = glossary.restore(protected.text, protected.replacements)

        self.assertEqual(restored, "宇宙之冠 receives a new effect.")



class ArticleContentServiceTests(SimpleTestCase):
    def setUp(self):
        self.glossary_loaders = [
            patch.object(WowNewsGlossary, method, return_value=WowNewsGlossary.empty())
            for method in ("from_builtin_terms", "from_active_talent_metadata")
        ]
        for loader in self.glossary_loaders:
            loader.start()
            self.addCleanup(loader.stop)

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

    def test_extract_wowhead_article_preserves_inline_paragraph_breaks(self):
        html = """
        <div id="news-post"><div class="text">
          First paragraph.<br><br>Second paragraph.<br>Continuation.
        </div></div>
        """

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertEqual(html_result.count("<br"), 3)

    def test_extract_wowhead_article_preserves_breaks_around_source_blocks(self):
        html = """
        <div id="news-post"><div class="text">
          Intro line.<br><br><table><tr><td>Data</td></tr></table><br><br>After table.
        </div></div>
        """

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertGreaterEqual(html_result.count("<br"), 4)

    def test_extract_wowhead_article_removes_layout_only_breaks(self):
        html = """
        <div id="news-post"><div class="text">
          First paragraph.<br><br>Second paragraph.<br><br>
          <div class="wh-center" style="text-align: center"><br></div><br><br>
          <blockquote>Missing trophies:<br><br>
            <ul><li><br><a href="/item=1">First trophy</a><br></li></ul><br>
            Follow-up paragraph.<br><br>Thank you.
          </blockquote>
        </div></div>
        """

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/382425", source="wowhead")
        html_result = blocks[0]["html"]
        soup = BeautifulSoup(html_result, "html.parser")

        self.assertEqual(html_result.count("<br"), 4)
        self.assertIsNone(soup.select_one(".wh-center"))
        self.assertIsNone(soup.select_one("li > br"))
        self.assertIn("First paragraph.<br/><br/>Second paragraph.", html_result)
        self.assertIn("Follow-up paragraph.<br/><br/>Thank you.", html_result)

        translated_html = html_result.replace("First paragraph.", "第一段。")
        self.assertEqual(normalize_wowhead_html_fragment(translated_html), translated_html)

    def test_extract_wowhead_article_preserves_semantic_source_breaks(self):
        html = """
        <div id="news-post"><div class="text">
          Intro line.<br><br>
          <ul>
            <li><br><b>July 2nd - July 6th:</b> Midnight Dungeons<br><br>The Blinding Vale<br></li>
            <li><br>Den of Nalorakk<br></li>
          </ul><br><br>
          <b>Dungeon Update</b><br><br>
          <ul><li><br><b>The Blinding Vale</b><br><br>General<br><br>Updated spawning.</li></ul>
        </div></div>
        """

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]
        soup = BeautifulSoup(html_result, "html.parser")

        self.assertIsNone(soup.select_one("li > br:first-child"))
        self.assertNotIn("<br/></li>", html_result)
        self.assertIn("Midnight Dungeons<br/><br/>The Blinding Vale", html_result)
        self.assertIn("<b>The Blinding Vale</b><br/><br/>General<br/><br/>Updated spawning.", html_result)
        self.assertIn("<b>Dungeon Update</b>", html_result)
        self.assertIn("Updated spawning.", blocks_to_plain_text(blocks))

    def test_extract_article_keeps_non_js_body_wrappers(self):
        html = """
        <article><style>.source-style { color: red }</style><nav>Source nav text</nav>
          <div class="source-wrapper">Body <span data-source="x">text</span></div>
        </article>
        """
        blocks = extract_structured_article(html, base_url="https://example.com/news/1", source="generic")
        result = blocks[0]["html"]
        self.assertIn("source-wrapper", result)
        self.assertIn("source-style", result)
        self.assertIn("Source nav text", result)

    def test_extract_article_preserves_format_attributes_but_removes_execution(self):
        html = """
        <article>
          <table class="diff-table" data-source="wowhead" style="width: 100%">
            <tr onclick="evil()"><td colspan="2" style="color: red" data-stat="haste">Value</td></tr>
          </table>
          <p class="note" style="font-weight: bold" data-kind="body">Keep <del>old</del><ins>new</ins></p>
          <a class="safe-link" href="/spell=1" onmouseover="evil()">Spell</a>
          <a class="bad-link" href="javascript:evil()">Bad</a>
          <img src="/image.png" onerror="evil()" data-source-src="/source.png" style="max-width: 100%">
        </article>
        """

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertIn('class="diff-table"', html_result)
        self.assertIn('data-source="wowhead"', html_result)
        self.assertIn('style="width: 100%"', html_result)
        self.assertIn('colspan="2"', html_result)
        self.assertIn('data-stat="haste"', html_result)
        self.assertIn('<del>old</del>', html_result)
        self.assertIn('<ins>new</ins>', html_result)
        self.assertIn('href="https://www.wowhead.com/spell=1"', html_result)
        self.assertIn('data-source-src="/source.png"', html_result)
        self.assertNotIn('onclick', html_result)
        self.assertNotIn('onmouseover', html_result)
        self.assertNotIn('onerror', html_result)
        self.assertNotIn('javascript:evil', html_result)

    def test_extract_wowhead_article_restores_empty_image_links(self):
        html = """
        <div id="news-post"><div class="text">
          <p>Trading Post rewards are live.</p>
          <a href="https://bnetcmsus-a.akamaihd.net/cms/content_entry_media/ay/IMAGE.png"></a>
          <a href="https://www.wowhead.com/item=1"></a>
        </div></div>
        """

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertIn('<img src="https://bnetcmsus-a.akamaihd.net/cms/content_entry_media/ay/IMAGE.png"/>', html_result)
        self.assertIn('href="https://bnetcmsus-a.akamaihd.net/cms/content_entry_media/ay/IMAGE.png"', html_result)
        self.assertIn('<a href="https://www.wowhead.com/item=1"></a>', html_result)

    def test_extract_wowhead_article_restores_screenshot_markup_from_script(self):
        html = r'''
        <script>WH.Gatherer.addData(91, 1, {"1290351":{"id":1290351,"imageType":3,"width":1024,"height":768},"1290356":{"id":1290356,"imageType":2,"width":1280,"height":720}});</script>
        <div class="news-post news-post-style-full" id="news-post-382066">
          <div class="news-post-content text">
            Raid maps for The Venomous Abyss have been updated on the Patch 12.1 PTR.
          </div>
        </div>
        <script>WH.markup.printHtml("Raid maps for The Venomous Abyss have been updated on the Patch 12.1 PTR.\r\n\r\n[center][screenshot id=1290351 width=800 size=normal alt=\"The Venomous Abyss raid map\"][\/screenshot][\/center]\r\n\r\n[screenshot id=1290356 width=400 size=normal alt=\"Second map\"][\/screenshot]");</script>
        '''

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/382066", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertIn('src="https://wow.zamimg.com/uploads/screenshots/normal/1290351.png"', html_result)
        self.assertIn('href="https://wow.zamimg.com/uploads/screenshots/normal/1290351.png"', html_result)
        self.assertIn('alt="The Venomous Abyss raid map"', html_result)
        self.assertIn('src="https://wow.zamimg.com/uploads/screenshots/normal/1290356.jpg"', html_result)
        self.assertIn("Raid maps for The Venomous Abyss", blocks_to_plain_text(blocks))

    def test_extract_wowhead_article_restores_item_table_cells_from_markup_script(self):
        html = r'''
        <script>WH.Gatherer.addData(3, 2, {"271884":{"name_enus":"Concentrated Silvermoon Health Potion","icon":"inv_health"},"271887":{"name_enus":"Liquid Luster","icon":"inv_luster"},"271890":{"name_enus":"Alluring Nostrum","icon":"inv_nostrum"}});</script>
        <div id="news-post"><div class="text">
          <p>Players will have more consumables to choose from in Patch 12.1.</p>
          <script>WH.markup.printHtml("[center][table][tr][td colspan=2 valign=top][center][item=271884 tooltip][\/center][\/td][\/tr][tr][td valign=top][item=271887 tooltip][\/td][td valign=top][item=271890 tooltip][\/td][\/tr][\/table][\/center]");</script>
          <noscript><br><table><br><tr><td colspan="2"></td></tr><br><tr><td></td><br><td></td></tr><br></table><br></noscript>
        </div></div>
        '''

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/382192", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertIn('href="https://www.wowhead.com/item=271884"', html_result)
        self.assertIn("Concentrated Silvermoon Health Potion", html_result)
        self.assertIn('src="https://wow.zamimg.com/images/wow/icons/large/inv_health.jpg"', html_result)
        self.assertIn("Liquid Luster", html_result)
        self.assertIn("Alluring Nostrum", html_result)
        self.assertEqual(html_result.count('class="wowhead-item-card"'), 3)
        self.assertIn('class="wh-center"', html_result)
        self.assertIn('style="text-align: center"', html_result)
        self.assertIn('colspan="2"', html_result)
        self.assertIn('style="vertical-align: top"', html_result)
        result_soup = BeautifulSoup(html_result, "html.parser")
        item_table = result_soup.find("table")
        self.assertIsNotNone(item_table)
        self.assertFalse(item_table.find_all("br", recursive=False))
        for row in item_table.find_all("tr"):
            self.assertFalse(row.find_all("br", recursive=False))

    def test_extract_wowhead_article_restores_spell_table_cells_from_markup_script(self):
        html = r'''
        <div id="news-post"><div class="text">
          <p>Datamined class tuning changes.</p>
          <script>WH.markup.printHtml("[table][tr][td][spell=162243 tempname=\"Demon's Bite\"][\/td][td][spell=179057 tempname=\"Chaos Nova\"][\/td][\/tr][\/table]");</script>
          <noscript><table><tr><td></td><td></td></tr></table></noscript>
        </div></div>
        '''

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertIn('href="https://www.wowhead.com/spell=162243"', html_result)
        self.assertIn("Demon's Bite", blocks_to_plain_text(blocks))
        self.assertIn('href="https://www.wowhead.com/spell=179057"', html_result)
        self.assertIn("Chaos Nova", blocks_to_plain_text(blocks))
        self.assertNotIn("<td></td>", html_result)

    def test_extract_wowhead_article_restores_del_markup_from_script(self):
        html = r'''
        <div id="news-post"><div class="text">
          <p>Datamined class tuning changes.</p>
          <script>WH.markup.printHtml("[table][tr][td]Value: [del]36[\/del][ins]30[\/ins][\/td][td]Duration: [del]8 sec[\/del][ins]6 sec[\/ins][\/td][\/tr][\/table]");</script>
          <noscript><table><tr><td>Value: 36 30</td><td>Duration: 8 sec 6 sec</td></tr></table></noscript>
        </div></div>
        '''

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertIn("<del>36</del>", html_result)
        self.assertIn("<ins>30</ins>", html_result)
        self.assertIn("<del>8 sec</del>", html_result)
        self.assertIn("<ins>6 sec</ins>", html_result)
        self.assertIn("Value:", blocks_to_plain_text(blocks))
        self.assertIn("36", blocks_to_plain_text(blocks))
        self.assertIn("30", blocks_to_plain_text(blocks))

    def test_extract_wowhead_article_normalizes_del_with_copy_attribute_in_html_text(self):
        html = '''
        <div id="news-post"><div class="text">
          <p>Damage: ([del copy=true]241.5%[/del]<ins>220%</ins>)</p>
        </div></div>
        '''

        blocks = extract_structured_article(html, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertIn("<del>241.5%</del><ins>220%</ins>", html_result)
        self.assertNotIn("[del copy=true]", html_result)
        self.assertNotIn("[/del]", html_result)

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

    def test_translate_blocks_skips_non_visible_html_text(self):
        blocks = [{"type": "html", "html": '<p>Patch Notes</p><svg><title>pulverize</title></svg><noscript>fallback</noscript>'}]
        pairs = [{"original": "Patch Notes", "translated": "补丁说明"}]

        translated = translate_blocks(loads_blocks(dumps_blocks(blocks)), pairs)

        self.assertIn("<p>补丁说明</p>", translated[0]["html"])
        self.assertIn("pulverize", translated[0]["html"])
        self.assertIn("fallback", translated[0]["html"])

    def test_polluted_html_block_translation_is_rejected(self):
        blocks = [{"type": "html", "html": "<p>A normal article paragraph.</p>"}]
        translated = [{"type": "html", "html": "<p>{}</p>".format(" ".join(["pulverize"] * 40))}]
        svc = ArticleTranslationService(engine=FakeEngine([]), sleep_func=lambda _: None)

        self.assertTrue(svc._blocks_look_polluted(blocks, translated))

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

    def test_upload_article_html_images_replaces_link_and_removes_source_attr(self):
        html = '<a class="article-image-link" href="https://cdn.example.com/original.jpg"><picture><source media="(min-width: 800px)" srcset="https://cdn.example.com/wide.jpg 1x"><img data-source-src="https://cdn.example.com/thumb.jpg" src="https://cdn.example.com/thumb.jpg" srcset="https://cdn.example.com/thumb-2x.jpg 2x" sizes="100vw"/></picture></a>'

        with patch("botend.services.article_image_service.download_and_upload_article_image", return_value="https://oss.wowdaily.cn/portal/articles/a.jpg"):
            result = upload_article_html_images(html, article_url="https://example.com/post/1", source="blizzard_tracker")

        self.assertIn('href="https://oss.wowdaily.cn/portal/articles/a.jpg"', result)
        self.assertIn('src="https://oss.wowdaily.cn/portal/articles/a.jpg"', result)
        self.assertNotIn("data-source-src", result)
        self.assertNotIn("srcset", result)
        self.assertNotIn("sizes", result)
        self.assertIn("<picture>", result)
        self.assertIn('<source media="(min-width: 800px)"/>', result)

    def test_upload_article_html_images_removes_external_source_attr_when_src_is_already_oss(self):
        html = '<img data-source-src="https://wow.zamimg.com/uploads/screenshots/normal/1.png" sizes="100vw" src="https://oss.wowdaily.cn/portal/articles/wowhead/1.png" srcset="https://wow.zamimg.com/uploads/screenshots/normal/1@2x.png 2x"/>'

        with patch("botend.services.article_image_service.download_and_upload_article_image") as mocked_upload:
            result = upload_article_html_images(html, article_url="https://www.wowhead.com/news/test", source="wowhead")

        self.assertIn('src="https://oss.wowdaily.cn/portal/articles/wowhead/1.png"', result)
        self.assertNotIn("data-source-src", result)
        self.assertNotIn("srcset", result)
        self.assertNotIn("sizes", result)
        mocked_upload.assert_not_called()

    def test_upload_article_images_replaces_href_only_images_and_reuses_cache(self):
        blocks = [
            {"type": "html", "html": '<a href="https://wow.zamimg.com/uploads/screenshots/normal/1.png"><img src="https://wow.zamimg.com/uploads/screenshots/normal/1.png"/></a>'},
            {"type": "html", "html": '<a href="https://wow.zamimg.com/uploads/screenshots/normal/1.png">full image</a>'},
        ]
        cache = {}

        with patch("botend.services.article_image_service.download_and_upload_article_image", return_value="https://oss.wowdaily.cn/portal/articles/wowhead/1.png") as mocked_upload:
            result = upload_article_images_in_blocks(blocks, article_url="https://www.wowhead.com/news/test", source="wowhead", upload_cache=cache)

        html_result = result[0]["html"] + result[1]["html"]
        self.assertNotIn("wow.zamimg.com", html_result)
        self.assertEqual(html_result.count("https://oss.wowdaily.cn/portal/articles/wowhead/1.png"), 3)
        self.assertEqual(mocked_upload.call_count, 1)
        self.assertEqual(cache["https://wow.zamimg.com/uploads/screenshots/normal/1.png"], "https://oss.wowdaily.cn/portal/articles/wowhead/1.png")

    def test_upload_article_images_replaces_original_html_images(self):
        blocks = [
            {
                "type": "html",
                "html": '<img src="https://oss.wowdaily.cn/portal/articles/wowhead/1.png"/>',
                "original_html": '<a href="https://wow.zamimg.com/uploads/screenshots/normal/1.png"><img src="https://wow.zamimg.com/uploads/screenshots/normal/1.png"/></a>',
            }
        ]

        with patch("botend.services.article_image_service.download_and_upload_article_image", return_value="https://oss.wowdaily.cn/portal/articles/wowhead/1.png") as mocked_upload:
            result = upload_article_images_in_blocks(blocks, article_url="https://www.wowhead.com/news/test", source="wowhead")

        self.assertNotIn("wow.zamimg.com", result[0]["original_html"])
        self.assertIn("oss.wowdaily.cn", result[0]["original_html"])
        self.assertEqual(mocked_upload.call_count, 1)

    @override_settings(PROXY_CONFIG={"http": "http://proxy.example:8883", "https": "http://proxy.example:8883"})
    def test_fetch_image_response_inherits_proxy_from_enabled_monitor_task(self):
        class FakeSession:
            proxies = {}

            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return object()

        class FakeTask:
            proxy_enabled = True

        class FakeReq:
            def __init__(self):
                self.s = FakeSession()
                self.current_task = FakeTask()

            def get_header(self, url, cookies, ext=None):
                return {"User-Agent": "Fake", **(ext or {})}

        req = FakeReq()
        _fetch_image_response("https://wow.zamimg.com/uploads/screenshots/normal/1.png", req=req)

        self.assertEqual(req.s.calls[0][1]["proxies"]["https"], "http://proxy.example:8883")
        self.assertIn("image/", req.s.calls[0][1]["headers"]["Accept"])

    @override_settings(PROXY_CONFIG={"http": "http://proxy.example:8883", "https": "http://proxy.example:8883"})
    def test_fetch_image_response_does_not_add_proxy_when_monitor_task_disabled(self):
        class FakeSession:
            proxies = {}

            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return object()

        class FakeTask:
            proxy_enabled = False

        class FakeReq:
            def __init__(self):
                self.s = FakeSession()
                self.current_task = FakeTask()

        req = FakeReq()
        _fetch_image_response("https://cdn.example.com/a.png", req=req)

        self.assertIsNone(req.s.calls[0][1]["proxies"])

    def test_extract_structured_article_normalizes_discourse_lightbox_structure(self):
        html = """
        <article>
          <p>Check out this image:</p>
          <div class="d-image-grid">
            <p><div class="lightbox-wrapper">
              <a class="lightbox" href="https://example.com/original.jpg" title="Screenshot">
                <img src="https://example.com/thumb.jpg" alt="Screenshot">
                <div class="meta">
                  <svg class="fa-icon" aria-hidden="true"><use href="#discourse-expand"></use></svg>
                  <span class="filename">screenshot.jpg</span>
                  <span class="informations">1920×1080 234 KB</span>
                </div>
              </a>
            </div></p>
          </div>
          <p>More text here.</p>
        </article>
        """

        blocks = extract_structured_article(html, base_url="https://example.com/post/1", source="blizzard_tracker")

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "html")
        html_result = blocks[0]["html"]
        self.assertNotIn('class="lightbox-wrapper"', html_result)
        self.assertNotIn('class="lightbox"', html_result)
        self.assertIn('class="article-image-link"', html_result)
        self.assertIn('href="https://example.com/original.jpg"', html_result)
        self.assertIn('<img alt="Screenshot" src="https://example.com/thumb.jpg"/>', html_result)
        self.assertNotIn('class="meta"', html_result)
        self.assertNotIn('class="filename"', html_result)
        self.assertNotIn('class="informations"', html_result)
        self.assertNotIn("<svg", html_result)
        self.assertNotIn("<p><div", html_result)
        self.assertIn("More text here.", blocks_to_plain_text(blocks))

    def test_normalize_blizzard_lightbox_preserves_non_chrome_descendants(self):
        html = """
        <div class="lightbox-wrapper">
          <a class="lightbox custom-link" href="/uploads/full.jpg">
            <picture>
              <source media="(min-width: 800px)" srcset="https://cdn.example.com/full.jpg">
              <img src="https://example.com/thumb.jpg" alt="Screenshot">
            </picture>
            <span class="credit">Artist credit</span>
            <div class="meta"><span class="filename">screenshot.jpg</span></div>
          </a>
          <figcaption>Meaningful caption</figcaption>
        </div>
        <div class="d-image-grid">
          <div class="lightbox-wrapper"><span>Fallback body without an image</span></div>
        </div>
        """

        result = normalize_blizzard_tracker_html_fragment(html)

        self.assertNotIn("lightbox-wrapper", result)
        self.assertNotIn('class="meta"', result)
        self.assertIn('class="custom-link article-image-link"', result)
        self.assertIn("<picture>", result)
        self.assertIn("Artist credit", result)
        self.assertIn("Meaningful caption", result)
        self.assertIn("Fallback body without an image", result)
