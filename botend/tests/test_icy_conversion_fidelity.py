"""Original fixtures for future HTML → Markdown → block conversion fidelity."""

from bs4 import BeautifulSoup, NavigableString
from django.test import SimpleTestCase

from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_icy import convert_page
from botend.services.class_guide_markdown import compile_markdown, html_to_markdown


URL = 'https://www.icy-veins.com/wow/test-conversion-fidelity'
CODE = 'alpha\n\n:::details\n**literal**\n:::end'


class IcyConversionFidelityTests(SimpleTestCase):
    def converted(self, source, icy):
        if icy:
            result = convert_page('<div class="guide-page-content">' + source + '</div>', URL)
            blocks = result['blocks']
            self.assertEqual(result['issues'], [])
        else:
            blocks = compile_markdown(html_to_markdown(source))
        return BeautifulSoup(''.join(b.get('html', '') for b in walk_blocks(blocks)),
                             'html.parser', preserve_whitespace_tags={'pre', 'code'})

    def test_break_runs_and_boundaries_survive_both_conversion_paths(self):
        for icy in (False, True):
            for body in ('甲<br>乙', '甲<br><br>乙', '<br>甲<br>',
                         '<br><br>甲<br><br>', '<br><br>', '甲<br><em>乙</em><br>丙'):
                with self.subTest(icy=icy, body=body):
                    expected = BeautifulSoup(body, 'html.parser')
                    actual = self.converted('<p>' + body + '</p>', icy)
                    # Text separated by explicit breaks, not just a total count.
                    def runs(node):
                        return [str(x) if isinstance(x, NavigableString) else '<br>'
                                for x in node.descendants
                                if isinstance(x, NavigableString) or x.name == 'br']
                    self.assertEqual(runs(actual), runs(expected))

    def test_ordered_starts_nested_content_and_breaks_are_not_reset(self):
        source = ('<ol start="4"><li>步骤甲<br><br>继续'
                  '<ul><li>子项一</li><li>子项二</li></ul>步骤甲后文</li>'
                  '<li>步骤乙<ol start="9"><li>内层步骤</li></ol></li></ol>')
        for icy in (False, True):
            with self.subTest(icy=icy):
                actual = self.converted(source, icy)
                self.assertEqual([x.get('start', '1') for x in actual.select('ol')], ['4', '9'])
                outer = actual.find('ol').find_all('li', recursive=False)
                self.assertEqual(len(outer), 2)
                self.assertEqual(outer[0].get_text(), '步骤甲继续子项一子项二步骤甲后文')
                self.assertEqual(len(outer[0].select('br')), 2)
                self.assertEqual([li.get_text() for li in outer[0].select('ul > li')], ['子项一', '子项二'])

    def test_carriage_return_prose_is_whitespace_not_a_new_paragraph(self):
        source = ('<p>甲\r\r<strong>乙</strong>\r丙。</p>'
                  '<p>真正的下一段。</p><pre><code>' + CODE + '</code></pre>')
        for icy in (False, True):
            with self.subTest(icy=icy):
                actual = self.converted(source, icy)
                self.assertEqual([p.get_text() for p in actual.select('p')], ['甲 乙 丙。', '真正的下一段。'])
                # CommonMark fenced code includes its terminal line ending.
                self.assertEqual(actual.select_one('pre code').get_text(), CODE + '\n')

    def test_nested_rows_match_html5_row_recovery_without_losing_cells_or_code(self):
        # Expected row/cell boundaries checked with Chromium DOMParser (HTML5).
        fixtures = [
            ('<table><tr><td rowspan="2">甲</td><td>乙</td>'
             '<tr><td colspan="2"><pre><code>' + CODE + '</code></pre></td></tr></tr></table>',
             [['甲', '乙'], [CODE]]),
            ('<table><tr><td>甲<tr><td>乙</td></tr><td>丙</td></tr></table>',
             [['甲'], ['乙'], ['丙']]),
            ('<table><tr><td>外<table><tr><td>内</td><tr><td>次</td></tr></tr>'
             '</table>尾</td><td>右</td></tr></table>', [['外内次尾', '右']]),
        ]
        for icy in (False, True):
            for source, expected in fixtures:
                with self.subTest(icy=icy, source=source):
                    actual = self.converted(source, icy)
                    table = actual.find('table')
                    rows = [tr for tr in table.find_all('tr') if tr.find_parent('table') is table]
                    self.assertEqual([[c.get_text() for c in tr.find_all(['td', 'th'], recursive=False)]
                                      for tr in rows], expected)
                    for tr in actual.select('tr'):
                        self.assertFalse(tr.find_parent('tr') and
                                         tr.find_parent('tr').find_parent('table') is tr.find_parent('table'))
                    if 'rowspan' in source:
                        self.assertEqual(actual.select_one('[rowspan]')['rowspan'], '2')
                        self.assertEqual(actual.select_one('[colspan]')['colspan'], '2')
                        self.assertEqual(actual.select_one('pre code').get_text(), CODE)
                        self.assertFalse(actual.select('strong'))
                    if '外' in source:
                        self.assertEqual([c.get_text() for c in actual.select('table table td')], ['内', '次'])
