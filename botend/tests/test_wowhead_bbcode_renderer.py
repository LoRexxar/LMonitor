from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from botend.services.article_content_service import blocks_to_plain_text, extract_structured_article
from botend.services.wowhead_bbcode_renderer import extract_wowhead_print_html_calls, render_wowhead_bbcode


class WowheadBBCodeRendererTests(SimpleTestCase):
    def test_extracts_javascript_string_without_executing_script(self):
        script = r'''WH.markup.printHtml("Hello \"quoted\" text\r\n[item=42 tooltip]", "target-id", {"uid":1});'''

        calls = extract_wowhead_print_html_calls(script)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target_id"], "target-id")
        self.assertEqual(calls[0]["markup"], 'Hello "quoted" text\r\n[item=42 tooltip]')

    def test_renders_nested_layout_entities_and_safe_attributes(self):
        markup = (
            "[db=live][db=ptr]Intro\r\n\r\n"
            "[center][table][tr][td colspan=2 valign=top][center]"
            "[item=271884 tooltip][/center][/td][/tr]"
            "[tr][td valign=top][spell=1289744 tempname=\"Potion Spell\"][/td]"
            "[td][npc=268228][/td][/tr][/table][/center]"
        )
        rendered = render_wowhead_bbcode(
            markup,
            base_url="https://www.wowhead.com/news/382192",
            entities={
                ("item", "271884"): {"name": "Health Potion", "icon": "inv_health"},
                ("npc", "268228"): {"name": "Jan'sari the Watchful", "url": "/npc=268228/jansari-the-watchful"},
            },
        )
        soup = BeautifulSoup(rendered, "html.parser")

        self.assertEqual(soup.find("td")["colspan"], "2")
        self.assertIn("vertical-align: top", soup.find("td")["style"])
        self.assertEqual(len(soup.select(".wh-center")), 2)
        self.assertIn("Health Potion", soup.get_text(" ", strip=True))
        self.assertIn("Potion Spell", soup.get_text(" ", strip=True))
        self.assertIn("Jan'sari the Watchful", soup.get_text(" ", strip=True))
        self.assertEqual(soup.select_one('[data-wh-entity="item"]')["data-wh-id"], "271884")
        self.assertEqual(len(soup.select("td .wowhead-item-card")), 1)
        self.assertNotIn("[db=", rendered)

    def test_preserves_unknown_wrapper_content_and_blocks_executable_urls(self):
        rendered = render_wowhead_bbcode(
            '[future-widget mode="x"]Visible [b]body[/b][/future-widget] '
            '[url=javascript:alert(1)]unsafe[/url] [url=/news/2]safe[/url] '
            '[url]https://www.wowhead.com/news/3[/url]',
            base_url="https://www.wowhead.com/news/1",
        )

        self.assertIn("Visible", rendered)
        self.assertIn("<strong>body</strong>", rendered)
        self.assertNotIn("future-widget", rendered)
        self.assertIn("unsafe", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn('href="https://www.wowhead.com/news/2"', rendered)
        self.assertIn('href="https://www.wowhead.com/news/3"', rendered)

    def test_renders_standard_image_bbcode_and_rejects_unsafe_source(self):
        rendered = render_wowhead_bbcode(
            '[img]/uploads/a.png[/img][img]javascript:alert(1)[/img]',
            base_url="https://www.wowhead.com/news/1",
        )

        self.assertIn('src="https://www.wowhead.com/uploads/a.png"', rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("[/img]", rendered)

    def test_renders_markdown_links_and_decodes_html_entities_in_text(self):
        rendered = render_wowhead_bbcode(
            "[收割](https://www.wowhead.com/spell=1226019) &nbsp;，"
            "[危险](javascript:alert(1))",
            base_url="https://www.wowhead.com/news/382254",
        )

        self.assertIn('href="https://www.wowhead.com/spell=1226019"', rendered)
        self.assertIn(">收割</a>", rendered)
        self.assertIn("\xa0，", rendered)
        self.assertNotIn("&amp;nbsp;", rendered)
        self.assertIn("危险", rendered)
        self.assertNotIn("javascript:", rendered)

    def test_renders_safe_html_wrapper_as_html_instead_of_escaped_text(self):
        rendered = render_wowhead_bbcode(
            '[html]<table><tr><th colspan="4">Class Tools</th></tr>'
            '<tr><th>Blood</th><td><a href="/ptr/talent-calc/death-knight/blood">Talents</a></td></tr>'
            '</table>[/html]',
            base_url="https://www.wowhead.com/news/382254",
        )
        soup = BeautifulSoup(rendered, "html.parser")

        self.assertIsNotNone(soup.find("table"))
        self.assertEqual(soup.find("th")["colspan"], "4")
        self.assertEqual(soup.find("a")["href"], "https://www.wowhead.com/ptr/talent-calc/death-knight/blood")
        self.assertNotIn("&lt;table&gt;", rendered)

    def test_html_wrapper_removes_executable_behavior_without_dropping_structure(self):
        rendered = render_wowhead_bbcode(
            '[html]<table onclick="alert(1)"><tr><td>Visible</td></tr></table>'
            '<script>alert(2)</script><a href="javascript:alert(3)" srcdoc="bad">Unsafe link</a>[/html]',
            base_url="https://www.wowhead.com/news/382254",
        )
        soup = BeautifulSoup(rendered, "html.parser")

        self.assertEqual(soup.find("td").get_text(strip=True), "Visible")
        self.assertIsNone(soup.find("script"))
        self.assertNotIn("onclick", soup.find("table").attrs)
        self.assertNotIn("href", soup.find("a").attrs)
        self.assertNotIn("srcdoc", soup.find("a").attrs)

    def test_keeps_screenshot_in_original_article_position(self):
        rendered = render_wowhead_bbcode(
            'Before screenshot.\r\n[screenshot id=1290351 width=800 alt="Raid map"][/screenshot]\r\nAfter screenshot.',
            base_url="https://www.wowhead.com/news/382066",
            screenshot_extensions={"1290351": "png"},
        )

        self.assertLess(rendered.index("Before screenshot."), rendered.index("wowhead-screenshot"))
        self.assertLess(rendered.index("wowhead-screenshot"), rendered.index("After screenshot."))
        self.assertIn("1290351.png", rendered)

    def test_article_extraction_uses_short_authoritative_print_html(self):
        source = r'''
        <div class="news-post-content text">
          <div id="short-target"></div>
          <script>WH.markup.printHtml("[b]Short[/b]", "short-target", {});</script>
          <noscript>Different and much longer fallback body that must not override BBCode.</noscript>
        </div>
        '''

        blocks = extract_structured_article(source, base_url="https://www.wowhead.com/news/1", source="wowhead")

        self.assertIn("<strong>Short</strong>", blocks[0]["html"])
        self.assertNotIn("Different and much longer", blocks[0]["html"])

    def test_article_extraction_handles_multiple_calls_with_independent_targets(self):
        source = r'''
        <div class="news-post-content text">
          <div id="first"></div><div id="second"></div>
          <script>WH.markup.printHtml("First body", "first", {}); WH.markup.printHtml("Second body", "second", {});</script>
          <noscript>Fallback text</noscript>
        </div>
        '''

        blocks = extract_structured_article(source, base_url="https://www.wowhead.com/news/1", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertEqual(html_result.count("First body"), 1)
        self.assertEqual(html_result.count("Second body"), 1)
        self.assertNotIn("Fallback text", html_result)

    def test_article_extraction_uses_print_html_as_authoritative_body(self):
        source = r'''
        <script>WH.Gatherer.addData(3, 2, {"271884":{"name_enus":"Health Potion","icon":"inv_health"}});</script>
        <div class="news-post-content text">
          <div id="article-target"></div>
          <script>WH.markup.printHtml("Intro.\r\n\r\n[center][table][tr][td colspan=2 valign=top][item=271884 tooltip][\/td][\/tr][\/table][\/center]\r\nAfter table.", "article-target", {"uid":382192});</script>
          <noscript>Intro.<br><br><table><br><tr><td></td></tr><br></table><br>After table.</noscript>
        </div>
        '''

        blocks = extract_structured_article(source, base_url="https://www.wowhead.com/news/382192", source="wowhead")
        html_result = blocks[0]["html"]

        self.assertEqual(html_result.count("Intro."), 1)
        self.assertEqual(html_result.count("After table."), 1)
        self.assertIn('colspan="2"', html_result)
        self.assertIn('data-wh-entity="item"', html_result)
        soup = BeautifulSoup(html_result, "html.parser")
        self.assertFalse(soup.find("table").find_all("br", recursive=False))
        self.assertFalse(soup.find("ul") and soup.find("ul").find_all("br", recursive=False))
        self.assertNotIn("<noscript", html_result)
        self.assertIn("After table.", blocks_to_plain_text(blocks))
