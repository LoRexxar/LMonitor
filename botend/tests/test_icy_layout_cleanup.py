"""Icy-only layout cleanup: empty steps and proven heading-number debris."""
from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from botend.services.class_guide_icy_layout import clean_icy_layout


def parse(markup):
    return BeautifulSoup(markup, 'html.parser', preserve_whitespace_tags={'pre', 'code'})


class IcyLayoutCleanupTests(SimpleTestCase):
    def test_empty_list_items_and_rows_only(self):
        soup = parse('<ol start="4"><li>施放技能</li><li> \n<br><span></span><!-- empty --></li>'
                     '<li><ul><li></li></ul></li></ol>'
                     '<table><tr><td>部位</td></tr><tr><td>&nbsp;</td><td><br></td></tr></table>')
        self.assertIs(clean_icy_layout(soup), soup)
        self.assertEqual([li.get_text() for li in soup.select('li')], ['施放技能'])
        self.assertEqual(soup.ol['start'], '4')
        self.assertEqual([tr.get_text() for tr in soup.select('tr')], ['部位'])
        before = str(soup)
        clean_icy_layout(soup)
        self.assertEqual(str(soup), before)

    def test_textless_semantic_nodes_are_not_empty_steps_or_rows(self):
        for markup in ('<img src="icon.png">', '<hr>', '<code>  </code>', '<pre>\n</pre>',
                       '<span class="guide-ref" data-reference-id="1"></span>',
                       '<span data-reference-kind="spell" data-reference-id="1"></span>',
                       '<ref id="spell-1"></ref>', '<a href="/spell/1"></a>',
                       '<span id="anchor"></span>', '<span aria-label="技能"></span>',
                       '<svg></svg>', '<iframe></iframe>'):
            with self.subTest(markup=markup):
                soup = parse(f'<ol><li>{markup}</li></ol><table><tr><td>{markup}</td></tr></table>')
                before = str(soup)
                clean_icy_layout(soup)
                self.assertEqual(str(soup), before)

    def test_number_before_nested_real_html_heading_is_absorbed(self):
        soup = parse('<ul><li><p>2.5.1.</p>\n<h4 id="mountain"><img src="hero.webp">山丘领主</h4>'
                     '<p>职业和专精天赋被重新排列和修改。</p></li></ul>'
                     '<p><em>3.2.</em></p><section><h5 id="deep">更深标题</h5><p>正文</p></section>')
        clean_icy_layout(soup)
        self.assertEqual(soup.h4['data-source-number'], '2.5.1.')
        self.assertEqual(soup.h5['data-source-number'], '3.2.')
        self.assertEqual([p.get_text() for p in soup.find_all('p')], ['职业和专精天赋被重新排列和修改。', '正文'])
        self.assertEqual(soup.h4['id'], 'mountain')
        self.assertEqual(soup.h4.img['src'], 'hero.webp')
        before = str(soup)
        clean_icy_layout(soup)
        self.assertEqual(str(soup), before)

    def test_repeated_confirmed_level_label_at_external_heading_boundary(self):
        # Frozen guide 82 has this paragraph at the end of md-73, before md-74.
        soup = parse('<p>战争模式说明。</p><p><strong>等级 20 起：</strong>2.1.</p>')
        heading = parse('<h4 id="md-74"><strong>等级 20 起：</strong>升级用战争模式天赋</h4>').h4
        clean_icy_layout(soup, following_heading=heading)
        self.assertEqual([p.get_text() for p in soup.find_all('p')], ['战争模式说明。'])
        self.assertEqual(heading['data-source-number'], '2.1.')
        self.assertEqual(heading.get_text(), '等级 20 起：升级用战争模式天赋')

    def test_numbers_prose_and_unproven_labels_are_preserved(self):
        cases = (
            '<p>2.3 倍伤害。</p><h3>标题</h3>',
            '<p>3.</p><p>施放技能。</p><h3>标题</h3>',
            '<p>3.</p>正文<h3>标题</h3>',
            '<p>3.</p><h2>主章节</h2>',
            '<p>3.</p><h4></h4>',
            '<p>3.</p><div>说明<h4>标题</h4></div>',
            '<p>3.</p><div><p>说明</p><h4>标题</h4></div>',
            '<p><code>3.</code></p><h4>标题</h4>',
            '<p>3.<img src="icon.png"></p><h4>标题</h4>',
            '<p><a href="/3">3.</a></p><h4>标题</h4>',
            '<p><span class="guide-ref">3.</span></p><h4>标题</h4>',
            '<p><strong>等级 20 起：</strong>3.</p><h4>升级循环</h4>',
            '<p><strong>等级 20 起：</strong>3.</p><h4><strong>等级 10 起：</strong>升级循环</h4>',
            '<p><strong>等级 20 起：</strong>3.施放技能。</p><h4><strong>等级 20 起：</strong>升级循环</h4>',
            '<p>3.</p><p></p><h4>标题</h4>',
            '<pre><p>3.</p><h4>代码示例</h4></pre>',
            '<pre><ol><li></li></ol><table><tr><td></td></tr></table></pre>',
            '<table><tr><td rowspan="2"></td></tr><tr><td>正文</td></tr></table>',
            '<table><tr><td rowspan="2">跨行正文</td></tr><tr><td></td></tr></table>',
        )
        for markup in cases:
            with self.subTest(markup=markup):
                soup = parse(markup)
                before = str(soup)
                clean_icy_layout(soup)
                self.assertEqual(str(soup), before)

    def test_external_heading_does_not_cross_nonempty_or_nested_tail(self):
        for markup in ('<p>3.</p><p>正文</p>', '<ul><li><p>3.</p></li></ul>', '<p>3.</p>正文'):
            with self.subTest(markup=markup):
                soup = parse(markup)
                before = str(soup)
                heading = parse('<h3>标题</h3>').h3
                clean_icy_layout(soup, following_heading=heading)
                self.assertEqual(str(soup), before)
                self.assertNotIn('data-source-number', heading.attrs)
