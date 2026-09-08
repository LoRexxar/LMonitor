from importlib import import_module

from bs4 import BeautifulSoup
from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import WowArticle, PortalNavigationGroup, PortalNavigationItem


class PortalNgaTests(TestCase):
    def article(self, **kwargs):
        return WowArticle.objects.create(**{'source': 'nga', 'category': 'nga', 'title': '普通帖子', **kwargs})

    def test_public_archive_search_filters_and_bounded_pages(self):
        wanted = self.article(title='前瞻冷帖', author='论坛用户', nga_board_id='310', nga_board_name='精英议会', reply_count=0,
                              content='正文检索词' + '长正文' * 10000)
        self.article(title='热门', category='hot', author=None)
        self.article(title='水区', author='论坛用户乙', nga_board_id='7', nga_board_name='艾泽拉斯议事厅')
        self.article(title='隐藏', is_active=False)
        self.article(title='其他来源', source='wowhead')
        for i in range(23):
            self.article(title=f'历史帖 {i}')
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/portal/nga/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page'].paginator.count, 26)
        self.assertEqual(len(response.context['page'].object_list), 20)
        self.assertContains(response, 'id="nga-swap"')
        # The SQL may inspect content for search/substring, but never transfers whole bodies.
        row_sql = [q['sql'] for q in queries if 'LIMIT 20' in q['sql']]
        self.assertTrue(row_sql)
        self.assertLessEqual(len(queries), 4)
        self.assertNotIn('"content"', row_sql[0].split('SUBSTR', 1)[0])
        self.assertNotIn('"content_blocks"', row_sql[0])
        response = self.client.get('/portal/nga/', {'q': '历史帖', 'sort': 'replies'})
        form = BeautifulSoup(response.content, 'html.parser').select_one('#nga-swap-form')
        params = {node['name']: node.get('value', '') for node in form.select('input[name]')}
        self.assertEqual(params['sort'], 'replies')
        second = self.client.get('/portal/nga/', params)
        self.assertEqual(len(second.context['page'].object_list), 3)
        self.assertEqual(second.context['q'], '历史帖')
        response = self.client.get('/portal/nga/', {'q': '正文检索词', 'board': '310'})
        self.assertContains(response, f'/portal/nga/{wanted.id}/')
        self.assertEqual(response.context['page'].paginator.count, 1)
        self.assertNotContains(response, '长正文' * 1000)
        self.assertContains(response, '精英议会')
        self.assertEqual(self.client.get('/portal/nga/', {'page': 'bad'}).status_code, 200)
        self.assertEqual(self.client.get('/portal/nga/', {'page': '999999999999999999999'}).status_code, 200)
        self.assertContains(self.client.get('/portal/nga/', {'q': '不存在'}), '没有匹配的帖子')

    def test_batch_replacement_fragment_filters_wrap_and_reader_return(self):
        from django.utils import timezone
        stamp = timezone.now()
        for i in range(23):
            self.article(title=f'刷帖 <script>bad()</script> {i}', nga_board_id='7',
                         nga_board_name='议事厅', publish_time=stamp, reply_count=i,
                         nga_replies_updated_at=stamp)
        self.article(title='不匹配', nga_board_id='310')
        first = self.client.get('/portal/nga/', {'q': '刷帖', 'board': '7', 'sort': 'replies'})
        soup = BeautifulSoup(first.content, 'html.parser')
        self.assertIsNone(soup.select_one('#nga-pagination'))
        self.assertIsNone(soup.select_one('#nga-total'))
        self.assertEqual(len(soup.select('.nga-post-link')), 20)
        self.assertIsNone(soup.select_one('.nga-read'))
        form = soup.select_one('#nga-swap-form')
        self.assertIsNotNone(form)
        params = {node['name']: node.get('value', '') for node in form.select('input[name]')}
        self.assertEqual(params['board'], '7')
        second = self.client.get('/portal/nga/', params, HTTP_X_NGA_BATCH='1')
        self.assertEqual(second.status_code, 200)
        fragment = BeautifulSoup(second.content, 'html.parser')
        self.assertIsNone(fragment.html)
        self.assertEqual(len(fragment.select('.nga-post-link')), 3)
        self.assertFalse({a['href'].split('?')[0] for a in soup.select('.nga-post-link')} &
                         {a['href'].split('?')[0] for a in fragment.select('.nga-post-link')})
        self.assertIn('X-NGA-Batch', second.headers['Vary'])
        self.assertIsNone(fragment.select_one('script'))
        self.assertIn('已看到末尾', fragment.get_text())
        detail = self.client.get(fragment.select_one('.nga-post-link')['href'])
        back = BeautifulSoup(detail.content, 'html.parser').select_one('#nga-back')['href']
        restored = self.client.get(back)
        self.assertEqual([x['id'] for x in restored.context['page']],
                         [x['id'] for x in second.context['page']])
        wrap = {node['name']: node.get('value', '') for node in fragment.select('#nga-swap-form input[name]')}
        again = self.client.get('/portal/nga/', wrap)
        self.assertEqual([x['id'] for x in again.context['page']], [x['id'] for x in first.context['page']])

    def test_detail_preserves_structure_and_blocks_xss_and_missing_content(self):
        article = self.article(title='<script>alert(1)</script>', url='javascript:alert(1)', content='''
<p onclick="alert(1)">段落<strong>重点</strong></p><blockquote>引用</blockquote>
<ul><li>列表</li></ul><table><tr><td>单元格</td></tr></table>
<img src="/attachments/a.png" onerror="alert(1)"><a href="java&#10;script:alert(1)">危险链接</a>
<script>alert(1)</script><iframe srcdoc="evil"></iframe><svg onload="alert(1)"></svg>
<style>body{display:none}</style><form action="https://evil.test"><button formaction="javascript:evil()">按钮</button></form>
''')
        response = self.client.get(f'/portal/nga/{article.id}/')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        body = soup.select_one('#nga-main-post')
        for tag in ['strong', 'blockquote', 'li', 'td', 'img']:
            self.assertIsNotNone(body.find(tag))
        self.assertIsNone(body.find(['script', 'iframe', 'svg', 'style', 'form']))
        self.assertNotIn('javascript:', str(body))
        self.assertNotIn('onerror', str(body))
        self.assertNotIn('onclick', str(body))
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertIsNone(soup.select_one('#nga-source-link'))
        missing = self.article(description='只有摘要，不能当主帖')
        self.assertContains(self.client.get(f'/portal/nga/{missing.id}/'), '尚未采集到主帖正文')
        hidden = self.article(is_active=False)
        other = self.article(source='wowhead')
        for pk in [hidden.id, other.id, 999999]:
            self.assertEqual(self.client.get(f'/portal/nga/{pk}/').status_code, 404)
        plain = self.article(content='第一段\n第二段 < 3')
        self.assertContains(self.client.get(f'/portal/nga/{plain.id}/'), '第二段 &lt; 3')

    def test_nga_attachment_images_and_readable_bbcode_summary(self):
        path = 'mon_202609/07/7Q76-bf30KoT1kSb3-6i.jpg'
        cdn = 'https://img.nga.cn/attachments/' + path
        item = self.article(content=(
            f'[img]./{path}[/img][url=./{path}]原图链接[/url][s:ac:哭笑]'
            f'<img src="https://external.test/{path}">'
            f'<source srcset="./{path} 2x">'
            '<img src="./mon_202609/07/document.pdf">'),
            description=f'摘要[img]./{path}[/img][url=https://example.com]链接文字[/url][s:ac:哭笑]')
        response = self.client.get(f'/portal/nga/{item.id}/')
        body = BeautifulSoup(response.content, 'html.parser').select_one('#nga-main-post')
        self.assertEqual(body.find_all('img')[0]['src'], cdn)
        self.assertEqual(body.find_all('img')[1]['src'], 'https://external.test/' + path)
        self.assertEqual(body.find_all('img')[2]['src'], 'https://bbs.nga.cn/mon_202609/07/document.pdf')
        self.assertEqual(body.source['srcset'], cdn + ' 2x')
        self.assertEqual(body.a['href'], 'https://bbs.nga.cn/' + path)
        self.assertIn('哭笑', body.get_text())
        self.assertNotIn('[s:ac:', str(body))
        summary = self.client.get('/portal/nga/').context['page'].object_list[0]['summary']
        self.assertIn('链接文字', summary)
        self.assertIn('哭笑', summary)
        for raw in ('[img]', '[url', '[s:', path):
            self.assertNotIn(raw, summary)

    def test_image_only_main_post_keeps_srcset_and_fallback_text(self):
        item = self.article(content='<picture><source srcset="/attachments/a.webp 1x">'
                            '<img alt="图片正文" srcset="/attachments/a.png 1x"></picture>')
        response = self.client.get(f'/portal/nga/{item.id}/')
        self.assertContains(response, 'id="nga-main-post"')
        body = BeautifulSoup(response.content, 'html.parser').select_one('#nga-main-post')
        self.assertEqual(body.img['alt'], '图片正文')
        self.assertEqual(body.img['srcset'], 'https://bbs.nga.cn/attachments/a.png 1x')
        self.assertEqual(body.source['srcset'], 'https://bbs.nga.cn/attachments/a.webp 1x')

    def test_malformed_source_urls_and_image_candidates_do_not_break_reader(self):
        item = self.article(url='https://[invalid', content='''
<p>正文仍然可读</p><a href="https://[invalid">坏链接</a>
<img src="https://[invalid" srcset="https://[invalid 1x, /attachments/good.png 2x">
<picture><source srcset="javascript:alert(1) 1x, /attachments/good.png 2x"></picture>
''')
        response = self.client.get(f'/portal/nga/{item.id}/')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        body = soup.select_one('#nga-main-post')
        self.assertIn('正文仍然可读', body.get_text())
        self.assertIsNone(soup.select_one('#nga-source-link'))
        self.assertFalse(body.a.has_attr('href'))
        self.assertFalse(body.img.has_attr('src'))
        self.assertEqual(body.img['srcset'], 'https://bbs.nga.cn/attachments/good.png 2x')
        self.assertNotIn('javascript:', str(body))

    def test_collector_preserves_full_main_post_and_never_substitutes_replies(self):
        from unittest.mock import Mock
        from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor
        monitor = object.__new__(PortalPostMonitor)
        main = '<p>完整正文' + '内容' * 1200 + '</p><blockquote>引用</blockquote>'
        monitor.req = Mock()
        monitor.req.get.return_value = Mock(status_code=200, text=f'<div id="postcontent0">{main}</div><div id="postcontent1">回复</div>')
        self.assertEqual(monitor._fetch_nga_main_post('https://bbs.nga.cn/read.php?tid=1'), main)
        monitor.req.get.return_value = Mock(status_code=200, text='<div id="postcontent1">只有回复</div>')
        self.assertEqual(monitor._fetch_nga_main_post('https://bbs.nga.cn/read.php?tid=1'), '')

    def test_home_api_stays_bounded_and_legacy_hover_stays_plain_text(self):
        item = self.article(category='hot', reply_count=50, content='<p>完整主楼</p>' + '<p>长正文</p>' * 1000)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/portal/api/nga-hot/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data'][0]['content_preview'][:4], '完整主楼')
        self.assertLessEqual(len(queries), 2)
        self.assertNotIn('"content"', queries[-1]['sql'].split('SUBSTR', 1)[0])
        hover = self.client.get(f'/portal/api/article/{item.id}/').json()['data']['content']
        self.assertTrue(hover.startswith('完整主楼'))
        self.assertNotIn('<p>', hover)

    def test_navigation_migration_and_home_entry(self):
        response = self.client.get('/')
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('#nga-more')['href'], '/portal/nga/')
        self.assertIsNotNone(soup.select_one('#portal-primary-nav a[href="/portal/nga/"]'))
        migration = import_module('botend.migrations.0207_move_nga_navigation')
        group = PortalNavigationGroup.objects.create(key='nga-test', name='测试')
        item = PortalNavigationItem.objects.create(group=group, name='自定义', url='/#section-nga', is_active=False)
        migration.move_navigation(apps, None)
        item.refresh_from_db()
        self.assertEqual(item.url, '/portal/nga/')
        self.assertEqual(item.name, '自定义')
        self.assertFalse(item.is_active)
        # Hydrated navigation uses the database API, not just the template fallback.
        active = PortalNavigationItem.objects.create(group=group, name='NGA 阅读', url='/#section-nga')
        migration.move_navigation(apps, None)
        payload = self.client.get('/portal/api/navigation/').json()['data']['items']
        self.assertTrue(any(row['url'] == '/portal/nga/' and row['name'] == 'NGA 阅读' for row in payload))

    def test_board_unknown_sort_order_and_old_records(self):
        from datetime import timedelta
        from django.utils import timezone
        old = self.article(title='历史低回复', author='论坛用户', nga_board_id='310', nga_board_name='精英议会', reply_count=0,
                           publish_time=timezone.now() - timedelta(days=1200))
        popular = self.article(title='高回复', author='论坛用户乙', nga_board_id='7', nga_board_name='艾泽拉斯议事厅', reply_count=500)
        null_board = self.article(author=None)
        named_author = self.article(author='论坛用户并非板块')
        response = self.client.get('/portal/nga/', {'sort': 'replies'})
        ids = [row['id'] for row in response.context['page']]
        self.assertEqual(ids[0], popular.id)
        self.assertIn(old.id, ids)
        unknown = self.client.get('/portal/nga/', {'board': 'unknown'})
        self.assertEqual({row['id'] for row in unknown.context['page']}, {null_board.id, named_author.id})
        known = self.client.get('/portal/nga/', {'board': '310'})
        self.assertEqual([row['id'] for row in known.context['page']], [old.id])
