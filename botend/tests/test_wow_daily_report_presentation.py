"""Contract regressions for the deterministic daily edition, not its collectors."""
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from botend.wow_daily_report.presentation import render_daily_document


class DailyReportPresentationTests(SimpleTestCase):
    report_date = date(2026, 11, 3)
    generated_at = datetime(2026, 11, 3, 2, 30, tzinfo=timezone.utc)

    def render(self, sections, renderers=None):
        return render_daily_document(
            self.report_date, sections, renderers or {}, generated_at=self.generated_at,
        )

    def test_full_edition_preserves_renderer_bodies_and_counts_with_short_navigation(self):
        keys = ['news', 'nga', 'videos', 'cutoffs', 'today', 'events', 'updates']
        short_names = ['新闻', '社区', '视频', '冲分', '北美轮换', '活动日历', '版本热修']
        sections, renderers, fragments, calls = [], {}, {}, []
        for index, key in enumerate(keys):
            items = [{'title': f'{key}-{number}'} for number in range(index + 1)]
            sections.append({'key': key, 'title': f'{key} 的完整长原标题', 'items': items, 'summary': f'{key} 有据摘要', 'count': 9999})
            fragments[key] = ''.join(
                f'<article class="daily-card"><h3>{item["title"]}</h3><div class="body-html">'
                f'<p>开头 {item["title"]}</p><ul><li>列表</li></ul><blockquote>引用</blockquote>'
                f'<table><tr><td>表格</td></tr></table><p>{"完整正文" * 700} 末尾 {item["title"]}</p>'
                '<img src="https://example.com/original.jpg" alt="原图"></div></article>'
                for item in items
            )
            def body_renderer(received, key=key, items=items):
                self.assertIs(received, items)
                calls.append(key)
                return fragments[key]
            renderers[key] = body_renderer

        document = self.render(sections, renderers)
        soup = BeautifulSoup(document, 'html.parser')
        self.assertTrue(document.lower().startswith('<!doctype html>'))
        self.assertEqual(soup.html['lang'], 'zh-CN')
        self.assertIn('width=device-width', soup.find('meta', attrs={'name': 'viewport'})['content'])
        self.assertEqual(calls, keys)
        self.assertEqual([node['data-section'] for node in soup.select('.daily-section')], keys)
        self.assertEqual([node.get_text(strip=True) for node in soup.select('.daily-nav a')], short_names)
        for index, (key, name) in enumerate(zip(keys, short_names)):
            with self.subTest(key=key):
                section = soup.select_one(f'section[data-section="{key}"]')
                self.assertEqual(section.h2.get_text(strip=True), f'{key} 的完整长原标题')
                self.assertEqual(section.h2['id'], f'section-{key}')
                self.assertEqual(soup.select_one(f'.daily-nav a[href="#section-{key}"]').get_text(strip=True), name)
                overview = soup.select_one(f'.edition-overview [data-section="{key}"]')
                self.assertEqual(overview.select_one('.overview-count').get_text(strip=True), str(index + 1))
                self.assertEqual(len(section.select('.daily-card')), index + 1)
                self.assertIn(fragments[key], document)
                self.assertIsNone(section.find('details'))
        self.assertEqual(len(soup.select('.section-summary')), 6)
        self.assertIsNone(soup.select_one('section[data-section="cutoffs"] .section-summary'))
        self.assertIn('2026-11-03 10:30:00', soup.select_one('.edition-meta').get_text())
        self.assertIn('北京时间', soup.select_one('.edition-meta').get_text())
        self.assertNotIn('阅读时长', soup.get_text())
        self.assertNotIn('日环比', soup.get_text())
        self.assertEqual(document, self.render(sections, renderers))
        self.assertFalse(soup.select('script, link[rel="stylesheet"]'))

    def test_new_sections_escape_notices_and_keep_time_region_and_safe_source_links(self):
        sections = [{
            'key': key, 'title': '<em>来源标题</em>', 'items': [{'title': '站内条目'}],
            'notice': '<script>alert("notice")</script> 并非全部地区',
            'source_time': '2026-11-03 08:00 <UTC+8>', 'region_label': '国服 & 欧服',
            'source_url': url,
        } for key, url in [
            ('today', 'https://example.com/today?region=cn&lang=zh'),
            ('events', '/portal/events/?day=2026-11-03'),
            ('updates', 'http://example.com/updates'),
        ]]
        soup = BeautifulSoup(self.render(sections), 'html.parser')
        for entry in sections:
            with self.subTest(key=entry['key']):
                section = soup.select_one(f'section[data-section="{entry["key"]}"]')
                self.assertEqual(section.h2.get_text(), entry['title'])
                self.assertFalse(section.select('script, em'))
                self.assertEqual(section.select_one('.section-notice').get_text(), entry['notice'])
                self.assertIn(entry['source_time'], section.select_one('.source-meta').get_text())
                self.assertIn(entry['region_label'], section.select_one('.source-meta').get_text())
                self.assertEqual(section.select_one('.source-link')['href'], entry['source_url'])

    def test_unsafe_source_urls_never_become_links(self):
        for url in ['javascript:alert(1)', 'data:text/html,secret', '//evil.test/path',
                    ' /\\evil.test/path', 'https://', 'https://[broken',
                    'java\nscript:alert(1)', 'https://good.test\n/evil', 'ftp://example.com/data']:
            with self.subTest(url=url):
                soup = BeautifulSoup(self.render([{
                    'key': 'today', 'title': '安排', 'items': [{'title': '条目'}], 'source_url': url,
                }]), 'html.parser')
                self.assertIsNone(soup.select_one('.source-link'))

    def test_source_notice_keeps_full_region_warning_without_repeating_its_time(self):
        notice = '北美正式服；来源日期 2026-11-01，北京时间 2026-11-02 10:00 收录。不是国服同日轮换。'
        soup = BeautifulSoup(self.render([{
            'key': 'today', 'items': [{'title': '轮换'}], 'notice': notice,
            'source_time': '2026-11-02 10:00', 'source_url': '/portal/',
        }]), 'html.parser')
        section = soup.select_one('.daily-today')
        self.assertEqual(section.select_one('.section-notice').get_text(), notice)
        self.assertEqual(section.get_text().count('2026-11-02 10:00'), 1)
        self.assertEqual(section.select_one('.source-link')['href'], '/portal/')
        self.assertIsNone(section.select_one('details'))

    def test_all_empty_sources_are_absent_and_renderers_are_not_called(self):
        keys = ['news', 'nga', 'videos', 'cutoffs', 'today', 'events', 'updates']
        sections = [{'key': key, 'title': key, 'items': [], 'notice': '来源暂未更新', 'summary': '过期摘要'}
                    for key in keys]
        calls = []
        def renderer(items):
            calls.append(items)
            return '<p>不应渲染空模块</p>'
        for entries in [sections, []]:
            with self.subTest(entries=bool(entries)):
                soup = BeautifulSoup(self.render(entries, dict.fromkeys(keys, renderer)), 'html.parser')
                self.assertFalse(soup.select('.daily-section, .daily-nav, .edition-overview'))
                self.assertEqual(len(soup.select('#edition-content .empty')), 1)
                self.assertIn('本期暂无收录内容', soup.select_one('#edition-content .empty').get_text())
                self.assertNotIn('过期摘要', soup.get_text())
                self.assertNotIn('来源暂未更新', soup.get_text())
        self.assertEqual(calls, [])

    def test_compact_navigation_omits_empty_sources_and_generation_time_is_in_footer(self):
        soup = BeautifulSoup(self.render([
            {'key': 'news', 'items': [], 'summary': '旧摘要'},
            {'key': 'events', 'items': [{'title': '活动'}]},
            {'key': 'today', 'items': [{'title': '轮换'}]},
            {'key': 'nga', 'items': []},
            {'key': 'cutoffs', 'items': []},
        ]), 'html.parser')
        self.assertEqual([node['data-section'] for node in soup.select('.daily-section')], ['events', 'today'])
        overview = soup.select_one('.edition-overview')
        self.assertIsNotNone(overview.select_one('.daily-nav'))
        self.assertEqual([node.get_text() for node in overview.select('.daily-nav a')], ['活动日历', '北美轮换'])
        self.assertEqual([node.get_text() for node in overview.select('.overview-unit')], ['项活动', '张卡片'])
        self.assertIsNone(soup.select_one('.daily-hero .edition-meta'))
        meta = soup.select_one('.edition-footer .edition-meta')
        self.assertIn('2026-11-03 10:30:00', meta.get_text())
        self.assertIn('北京时间', meta.get_text())
        self.assertIn('UTC+8', meta.get_text())
        self.assertIn('CST', meta.get_text())
        self.assertNotIn('旧摘要', soup.get_text())

    def test_unknown_sections_use_safe_generic_cards_without_internal_payloads(self):
        soup = BeautifulSoup(self.render([{
            'key': 'future"><script>bad</script>', 'title': '站内新内容',
            'items': [
                {'title': '可读条目 <b>一</b>', 'description': '完整说明 ' * 500,
                 'internal_payload': {'secret': 'do-not-display'}, 'body': {'raw': 'never-dump'}},
                {'title': '第二条', 'description': {'secret': 'no-json'}, 'body_html': '<script>not-trusted</script>'},
            ],
        }]), 'html.parser')
        section = soup.select_one('.daily-section')
        self.assertEqual(len(section.select('.daily-card')), 2)
        self.assertIn('可读条目 <b>一</b>', section.get_text())
        self.assertIn('完整说明 ' * 499, section.get_text())
        self.assertNotIn('secret', soup.get_text())
        self.assertNotIn('not-trusted', soup.get_text())
        self.assertFalse(soup.select('script, b'))
        self.assertEqual(soup.select_one('.daily-nav a')['href'], '#' + section.h2['id'])
        self.assertEqual(soup.select_one('.overview-count').get_text(), '2')

    def test_all_plain_metadata_is_escaped_and_duplicate_keys_have_unique_anchors(self):
        sections = [{'key': 'news', 'title': '新闻', 'items': [{'title': '条目'}], 'summary': '<img src=x onerror=bad>'},
                    {'key': 'news', 'title': '第二组', 'items': [{'title': '条目二'}]}]
        document = render_daily_document('<bad date>', sections, {}, generated_at='<bad time>')
        soup = BeautifulSoup(document, 'html.parser')
        self.assertFalse(soup.select('bad, img'))
        self.assertIn('<bad date>', soup.title.get_text())
        self.assertIn('<bad time>', soup.select_one('.edition-meta').get_text())
        self.assertEqual([node['id'] for node in soup.select('.daily-section h2')], ['section-news', 'section-news-2'])
        self.assertEqual([node['href'] for node in soup.select('.daily-nav a')], ['#section-news', '#section-news-2'])

    def test_colliding_source_keys_cannot_create_duplicate_ids(self):
        soup = BeautifulSoup(self.render([
            {'key': key, 'items': [{'title': '公开条目'}]} for key in ['news', 'news-2', 'news']
        ]), 'html.parser')
        ids = [node['id'] for node in soup.select('[id]')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([node['id'] for node in soup.select('.daily-section h2')],
                         ['section-news', 'section-news-2', 'section-news-3'])
        for link in soup.select('.daily-nav a'):
            self.assertIsNotNone(soup.find(id=link['href'][1:]))
