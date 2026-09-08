from django.test import TestCase
from bs4 import BeautifulSoup
from botend.models import WowArticle
from botend.services.nga_browse_service import render_main_post

class NgaFactsTests(TestCase):
    def test_bbcode_nested_mixed_and_xss(self):
        body = BeautifulSoup(render_main_post('<p>[quote]外层[quote][b]内层[/b][/quote][/quote][list][*]一[*]二[/list][url=javascript:alert(1)]危险[/url]<img src="/a.png" onerror="evil()"></p>'), 'html.parser')
        self.assertEqual(len(body.select('blockquote blockquote strong')), 1)
        self.assertEqual(len(body.select('li')), 2)
        self.assertNotIn('javascript:', str(body))
        self.assertNotIn('onerror', str(body))

    def test_listing_and_main_facts(self):
        from botend.services.nga_facts_service import parse_listing, parse_main_post, apply_facts
        source = '''<a class="nav_link" href="/thread.php?fid=7">艾泽拉斯议事厅 - Hall of Azeroth</a><table id="topicrows"><tbody><tr><td><a class="replies">0</a></td><td><a class="topic" href="/read.php?tid=47518542">主帖</a></td><td><a class="author">潦懆</a></td></tr></tbody></table>'''
        row = parse_listing(source)[0]
        self.assertEqual(row['facts']['author'], '潦懆')
        self.assertEqual(row['facts']['reply_count'], 0)
        a = WowArticle.objects.create(source='nga', reply_count=99, url=row['url'])
        apply_facts(a, row['facts']); a.refresh_from_db()
        self.assertEqual(a.reply_count, 0)
        self.assertIsNotNone(a.nga_replies_updated_at)
        raw = '''<a class="nav_link" href="/thread.php?fid=7">艾泽拉斯议事厅</a><a id="postauthor0" href="nuke.php?func=ucp&uid=61343386"></a><p id="postcontent0">[b]主楼[/b]</p><script>commonui.userInfo.setAll({"61343386":{"username":"潦懆"}})</script>'''
        facts = parse_main_post(raw)
        self.assertEqual(facts['author'], '潦懆')
        self.assertEqual(facts['content'], '[b]主楼[/b]')
        self.assertNotIn('reply_count', facts)
        self.assertEqual(parse_main_post('<p id="postcontent1">回复</p>'), {})

    def test_legacy_classification_is_not_a_fact(self):
        a = WowArticle.objects.create(source='nga', author='nga前瞻区', title='历史')
        response = self.client.get(f'/portal/nga/{a.pk}/')
        self.assertContains(response, '作者未记录')
        self.assertContains(response, '板块未记录')
        self.assertContains(response, '回复数未核验')

    def test_preview_api_survives_real_author_backfill(self):
        from django.test import RequestFactory
        from botend.portal.api import PortalExwindLatestAPIView
        import json
        a = WowArticle.objects.create(source='nga', category='nga', author='真实用户', nga_board_id='310', title='前瞻')
        response = PortalExwindLatestAPIView.as_view()(RequestFactory().get('/', {'source': 'nga_preview'}))
        self.assertIn(a.id, [row['id'] for row in json.loads(response.content)['data']])

    def test_backfill_dry_run_apply_and_unresolved_preserve_facts(self):
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command
        a = WowArticle.objects.create(source='nga', url='https://bbs.nga.cn/read.php?tid=123', content='旧主楼', reply_count=77)
        listing = '<a class="nav_link" href="/thread.php?fid=310">精英议会</a><table id="topicrows"><tbody><a class="topic" href="/read.php?tid=123">标题</a><a class="replies">0</a></tbody></table>'
        main = '<p id="postcontent0">[b]完整主楼[/b]</p><a id="postauthor0">作者甲</a>'
        with patch('botend.management.commands.backfill_nga_facts.fetch_page', side_effect=lambda url, cookie: listing if 'thread.php' in url else main), patch('botend.management.commands.backfill_nga_facts.time.sleep'):
            call_command('backfill_nga_facts', limit=1, stdout=StringIO())
            a.refresh_from_db(); self.assertEqual(a.content, '旧主楼'); self.assertEqual(a.reply_count, 77)
            call_command('backfill_nga_facts', limit=1, apply=True, stdout=StringIO())
            a.refresh_from_db(); self.assertEqual(a.content, '[b]完整主楼[/b]'); self.assertEqual(a.reply_count, 0)
            self.assertEqual(a.author, '作者甲'); self.assertIsNotNone(a.nga_replies_updated_at)
        with patch('botend.management.commands.backfill_nga_facts.fetch_page', return_value='<html>challenge</html>'), patch('botend.management.commands.backfill_nga_facts.time.sleep'):
            output = StringIO()
            call_command('backfill_nga_facts', limit=1, apply=True, stdout=output, stderr=StringIO())
            a.refresh_from_db(); self.assertEqual(a.content, '[b]完整主楼[/b]'); self.assertEqual(a.reply_count, 0)
            self.assertIn('fetch_failures=1', output.getvalue())

    def test_portal_refresh_preserves_batch_after_one_main_post_failure(self):
        from unittest.mock import Mock, patch
        from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor
        listing = '<a class="nav_link" href="/thread.php?fid=7">议事厅</a><table id="topicrows">' + ''.join(f'<tbody><a class="topic" href="/read.php?tid={i}">标题{i}</a><a class="replies">0</a></tbody>' for i in range(1, 33)) + '</table>'
        def fetch(url, cookie):
            if 'thread.php' in url:
                return listing
            if url.endswith('=1'):
                raise ValueError('Unavailable')
            return '<p id="postcontent0">[b]主楼[/b]</p>'
        monitor = PortalPostMonitor(Mock(is_chrome=False), Mock())
        with patch('botend.services.nga_facts_service.fetch_page', side_effect=fetch), patch('botend.controller.plugins.portal.PortalPostMonitor.upsert_system_alert') as alert:
            monitor.update_nga_hot()
        self.assertEqual(WowArticle.objects.filter(source='nga').count(), 30)
        self.assertEqual(WowArticle.objects.get(url='https://bbs.nga.cn/read.php?tid=2').content, '[b]主楼[/b]')
        alert.assert_called_once()

    def test_real_detail_controls_and_verified_reply_signature(self):
        from botend.services.nga_facts_service import parse_main_post
        raw = '''<p id="postcontent0">主楼</p><a id="postauthor0" href="nuke.php?uid=61343386"></a>
<script>var read="/js_read.js?3587966";
commonui.userInfo.setAll({"61343386":{"username":"潦懆","remark":"sv\twow"}});
commonui.postArg.setDefault(7,0,47518542,61343386,0,"","",",12,34,","",null,0,5,1788837722,6)
if(commonui.beforePostProc)commonui.beforePostProc()
</script>'''
        facts = parse_main_post(raw)
        self.assertEqual(facts['author'], '潦懆')
        self.assertEqual(facts['reply_count'], 5)
        self.assertIn('nga_replies_updated_at', facts)
        self.assertEqual(parse_main_post(raw.replace(',0,5,1788837722', ',0,0,1788837722'))['reply_count'], 0)
        for invalid in (raw.replace('3587966', 'unknown'), raw.replace(',0,5,1788837722', ',0,-1,1788837722'), raw.replace(',0,5,1788837722', ',0,true,1788837722'), raw.replace(',0,5,1788837722', ',0,evil(),1788837722')):
            self.assertNotIn('reply_count', parse_main_post(invalid))

    def test_backfill_latest_and_independent_bounded_listing_updates(self):
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command
        articles = [WowArticle.objects.create(source='nga', url=f'https://bbs.nga.cn/read.php?tid={i}', content='old', reply_count=99) for i in range(3)]
        listing = '<table id="topicrows">' + ''.join(f'<tbody><a class="topic" href="/read.php?tid={i}">标题</a><a class="author">源站作者</a><a class="replies">0</a></tbody>' for i in range(2)) + '</table>'
        def fetch(url, cookie):
            return listing if 'thread.php' in url else '<p id="postcontent0">new</p>'
        with patch('botend.management.commands.backfill_nga_facts.fetch_page', side_effect=fetch) as request, patch('botend.management.commands.backfill_nga_facts.time.sleep'):
            out = StringIO()
            call_command('backfill_nga_facts', limit=1, listing_limit=1, stdout=out)
            for a in articles:
                a.refresh_from_db(); self.assertEqual(a.content, 'old'); self.assertEqual(a.reply_count, 99)
            self.assertEqual(request.call_args.args[0], articles[2].url)
            call_command('backfill_nga_facts', '--newest', limit=1, listing_limit=1, apply=True, stdout=out)
            for a in articles:
                a.refresh_from_db()
            self.assertEqual(articles[0].reply_count, 99)
            self.assertEqual(articles[1].reply_count, 0)
            self.assertEqual(articles[1].author, '源站作者')
            self.assertEqual(articles[1].content, 'old')
            self.assertEqual(articles[2].content, 'new')
            self.assertIn('listing_applied=1', out.getvalue())
            self.assertIn('listing_skipped_by_limit=1', out.getvalue())
            call_command('backfill_nga_facts', '--oldest', limit=1, stdout=StringIO())
            self.assertEqual(request.call_args.args[0], articles[0].url)

    def test_bbcode_table_without_other_tags(self):
        self.assertIn('<td>内容</td>', render_main_post('[table][tr][td]内容[/td][/tr][/table]'))
