"""攻略快照的只读、更新、回滚、并发与正文保真回归。"""
import io
from pathlib import Path
import uuid
import threading
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, SimpleTestCase, RequestFactory, override_settings

from botend.guide_models import ClassGuide, ClassGuideTag
from botend.portal.class_guides import PortalClassGuideArticleView, PortalClassGuideCatalogView
from botend.services import class_guide_snapshot as files
from botend.services.class_guide_service import save_article


class GuideWorkerRetirementTests(SimpleTestCase):
    def test_retirement_finishes_current_build_and_prevents_another_cycle(self):
        from botend.backend_lifecycle import BackendLifecycle
        lifecycle = BackendLifecycle()
        started, finish = threading.Event(), threading.Event()

        def build(**kwargs):
            started.set()
            if not finish.wait(5):
                raise AssertionError('测试未释放正在生成的攻略')

        with (patch.object(files, '_thread', None),
              patch.object(files, 'close_old_connections'),
              patch.object(files, 'refresh_guide_snapshot', side_effect=build) as refresh):
            worker = files.start_guide_snapshot_worker(lifecycle=lifecycle)
            try:
                self.assertTrue(started.wait(2))
                self.assertIs(files.start_guide_snapshot_worker(lifecycle=lifecycle), worker)
                lifecycle.request_retirement()
                self.assertTrue(worker.is_alive(), '退出时必须让已开始的构建完成')
            finally:
                finish.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            refresh.assert_called_once_with(poll=True)


@override_settings(SECURE_SSL_REDIRECT=False)
class GuideSnapshotTests(TestCase):
    def setUp(self):
        self.root = Path('tmp/class-guides-snapshot-tests') / uuid.uuid4().hex
        override = override_settings(CLASS_GUIDE_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(files._cached_content.cache_clear)
        self.addCleanup(files._cached_html.cache_clear)
        self.guide = ClassGuide.objects.create(title='奥术完整攻略', slug='arcane-test', spec_id=62,
            game_version='current', content_markdown='## 中文章节\n\n完整正文\n\n:::details 补充说明\n折叠内容\n:::',
            source_markdown='不应编译的英文原文')

    def request(self, article=False):
        request = RequestFactory().get('/portal/class-guides/')
        request.user = AnonymousUser()
        view = PortalClassGuideArticleView if article else PortalClassGuideCatalogView
        return view.as_view()(request, **({'guide_id': self.guide.pk} if article else {}))

    def test_public_reads_have_no_sql_or_compilation_and_preserve_complete_html(self):
        files.refresh_guide_snapshot()
        before = {p.name: p.stat().st_mtime_ns for p in self.root.iterdir()}
        with (self.assertNumQueries(0),
              patch.object(files, 'resolve_references', side_effect=AssertionError('禁止现场解析')),
              patch.object(files, '_content', side_effect=AssertionError('禁止读取正文JSON')),
              patch.object(files, 'render_to_string', side_effect=AssertionError('禁止现场渲染')),
              patch('botend.portal.class_guides.render_to_string', side_effect=AssertionError('匿名禁止现场渲染'))):
            for _ in range(2):
                files._cached_html.cache_clear()
                catalog = self.request()
                article = self.request(article=True)
                self.assertContains(catalog, '奥术完整攻略')
                for text in ('中文章节', '完整正文', '折叠内容', '本篇目录'):
                    self.assertContains(article, text)
        self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in self.root.iterdir()})
        data = files.read_guide_snapshot()
        self.assertNotIn('content_markdown', data['rows'][0]['guide'])
        self.assertNotIn('source_payload', data['rows'][0]['guide'])

    def test_missing_snapshot_returns_503_without_query_or_write(self):
        with self.assertNumQueries(0):
            self.assertEqual(self.request().status_code, 503)
            self.assertEqual(self.request(article=True).status_code, 503)
        self.assertFalse(self.root.exists())

    def test_failed_build_keeps_last_complete_publication(self):
        files.refresh_guide_snapshot()
        before = (self.root / 'index.json').read_bytes()
        self.guide.content_markdown = '新正文'
        self.guide.save()
        with patch.object(files, '_article', side_effect=RuntimeError('模拟构建失败')):
            with self.assertRaises(RuntimeError):
                files.refresh_guide_snapshot()
        self.assertEqual(before, (self.root / 'index.json').read_bytes())
        self.assertIn('完整正文', files.read_guide_snapshot(self.guide.pk)['article_html'])
        files.refresh_guide_snapshot()
        self.assertIn('新正文', files.read_guide_snapshot(self.guide.pk)['article_html'])

    def test_catalog_article_related_and_archive_switch_together(self):
        other = ClassGuide.objects.create(title='另一篇', slug='other', spec_id=62, game_version='current')
        files.refresh_guide_snapshot()
        self.assertEqual(len(files.read_guide_snapshot(self.guide.pk)['related']), 2)
        self.guide.archived = True
        self.guide.save(update_fields=['archived'])
        files.refresh_guide_snapshot()
        with self.assertNumQueries(0):
            self.assertIsNone(files.read_guide_snapshot(self.guide.pk))
            self.assertEqual(len(files.read_guide_snapshot()['rows']), 1)
            self.assertEqual(len(files.read_guide_snapshot(other.pk)['related']), 1)
        self.assertEqual(self.client.get(f'/portal/class-guides/{self.guide.pk}/').status_code, 404)

    def test_notice_is_commit_only_and_save_article_update_is_covered(self):
        marker = self.root / 'revision.json'
        with self.captureOnCommitCallbacks(execute=True):
            save_article(self.guide.pk, '保存后标题', content_markdown='保存后正文', expected_updated_at=self.guide.updated_at)
            self.assertFalse(marker.exists())
        self.assertTrue(marker.exists())
        before = marker.read_bytes()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.guide.title = '回滚标题'
                    self.guide.save()
                    raise ValueError('回滚')
            except ValueError:
                pass
        self.assertEqual(marker.read_bytes(), before)
        files.refresh_guide_snapshot()
        self.assertEqual(files.read_guide_snapshot()['rows'][0]['title'], '保存后标题')

    def test_tag_author_and_disclaimer_updates_refresh_projection(self):
        files.refresh_guide_snapshot()
        with self.captureOnCommitCallbacks(execute=True):
            tag = ClassGuideTag.objects.create(name='测试标签', disclaimer='<script>仅作文本</script>')
            self.guide.tags.add(tag)
            self.guide.author_profile = {'name': '编辑作者'}
            self.guide.save(update_fields=['author_profile'])
        files._write(self.root / 'attempt.json', {'at': 0})
        self.assertTrue(files.refresh_guide_snapshot(poll=True))
        self.assertContains(self.request(article=True), '编辑作者')
        self.assertContains(self.request(article=True), '&lt;script&gt;')
        self.assertEqual(files.read_guide_snapshot()['tags'], ['测试标签'])
        with self.captureOnCommitCallbacks(execute=True):
            self.guide.tags.clear()
        files._write(self.root / 'attempt.json', {'at': 0})
        files.refresh_guide_snapshot(poll=True)
        self.assertNotContains(self.request(article=True), '仅作文本')

    def test_concurrent_change_does_not_publish_mixed_generation(self):
        files.refresh_guide_snapshot()
        before = (self.root / 'index.json').read_bytes()
        original = files._article
        def changed(guide, row):
            result = original(guide, row)
            files._write(self.root / 'revision.json', {'revision': '并发修改'})
            return result
        with patch.object(files, '_article', side_effect=changed):
            self.assertFalse(files.refresh_guide_snapshot())
        self.assertEqual(before, (self.root / 'index.json').read_bytes())

    def test_incremental_build_reuses_unchanged_articles_and_periodically_refreshes_references(self):
        files.refresh_guide_snapshot()
        with patch.object(files, '_article', wraps=files._article) as build:
            files._write(self.root / 'checked.json', {'at': 0, 'full_at': files.time.time()})
            files._write(self.root / 'attempt.json', {'at': 0})
            files.refresh_guide_snapshot(poll=True)
            build.assert_not_called()
            files._write(self.root / 'checked.json', {'at': 0, 'full_at': 0})
            files._write(self.root / 'attempt.json', {'at': 0})
            files.refresh_guide_snapshot(poll=True)
            build.assert_called_once()

    def test_corrupt_file_is_not_rendered_and_background_repairs_it(self):
        files.refresh_guide_snapshot()
        index = files._load(self.root / 'index.json')
        path = self.root / index['articles'][str(self.guide.pk)]['html']
        path.write_text('<html>不可信改写</html>', encoding='utf-8')
        self.assertEqual(self.request(article=True).status_code, 503)
        files.refresh_guide_snapshot()
        self.assertContains(self.request(article=True), '完整正文')

    def test_publish_lock_and_command(self):
        with files._lock(self.root / 'publish.lock'):
            self.assertFalse(files.refresh_guide_snapshot())
        output = io.StringIO()
        call_command('refresh_class_guide_snapshots', stdout=output)
        self.assertIn('已发布', output.getvalue())

    def test_unnotified_bulk_update_is_picked_up_by_periodic_reconciliation(self):
        files.refresh_guide_snapshot()
        ClassGuide.objects.filter(pk=self.guide.pk).update(content_markdown='批量更新正文')
        self.assertIn('完整正文', files.read_guide_snapshot(self.guide.pk)['article_html'])
        files._write(self.root / 'checked.json', {'at': 0, 'full_at': files.time.time()})
        files._write(self.root / 'attempt.json', {'at': 0})
        files.refresh_guide_snapshot(poll=True)
        self.assertIn('批量更新正文', files.read_guide_snapshot(self.guide.pk)['article_html'])

    def test_delete_and_tag_save_notify_worker(self):
        tag = ClassGuideTag.objects.create(name='免责声明')
        self.guide.tags.add(tag)
        files.refresh_guide_snapshot()
        with self.captureOnCommitCallbacks(execute=True):
            tag.disclaimer = '新版声明'
            tag.save(update_fields=['disclaimer'])
        files._write(self.root / 'attempt.json', {'at': 0})
        files.refresh_guide_snapshot(poll=True)
        self.assertEqual(files.read_guide_snapshot(self.guide.pk)['disclaimers'], ['新版声明'])
        with self.captureOnCommitCallbacks(execute=True):
            self.guide.delete()
        files._write(self.root / 'attempt.json', {'at': 0})
        files.refresh_guide_snapshot(poll=True)
        self.assertEqual(files.read_guide_snapshot()['rows'], [])

    def test_full_html_is_served_verbatim_and_metadata_contains_no_article_html(self):
        files.refresh_guide_snapshot()
        index = files._load(self.root / 'index.json')
        entry = index['articles'][str(self.guide.pk)]
        self.assertEqual(self.request(article=True).content, (self.root / entry['html']).read_bytes())
        self.assertEqual(self.request().content, (self.root / index['catalog_page']['html']).read_bytes())
        metadata = files._load(self.root / entry['file'])
        self.assertNotIn('article_html', metadata)
        self.assertTrue(metadata['body_file'].endswith('.html'))
        self.assertNotIn('csrfmiddlewaretoken', (self.root / entry['html']).read_text(encoding='utf-8'))

    def test_authenticated_fragments_do_not_leak_between_users_or_into_files(self):
        files.refresh_guide_snapshot()
        index_before = (self.root / 'index.json').read_bytes()
        original = self.request(article=True).content
        editor = get_user_model().objects.create_superuser(username='专属编辑', password='test-only')
        reader = get_user_model().objects.create_user(username='普通读者')
        url = f'/portal/class-guides/{self.guide.pk}/'
        self.client.force_login(editor)
        response = self.client.get(url)
        self.assertContains(response, '专属编辑')
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertContains(response, f'&amp;guide={self.guide.pk}')
        self.client.force_login(reader)
        response = self.client.get(url)
        self.assertContains(response, '普通读者')
        self.assertNotContains(response, '专属编辑')
        self.assertNotContains(response, '?section=class-guides')
        self.assertEqual(self.request(article=True).content, original)
        self.assertNotIn('专属编辑'.encode(), original)
        self.assertNotIn('普通读者'.encode(), original)
        self.assertEqual((self.root / 'index.json').read_bytes(), index_before)

    def test_related_html_updates_even_when_unchanged_body_is_reused(self):
        other = ClassGuide.objects.create(title='原推荐标题', slug='related', spec_id=62, game_version='current')
        files.refresh_guide_snapshot()
        index = files._load(self.root / 'index.json')
        old_body = index['articles'][str(self.guide.pk)]['file']
        ClassGuide.objects.filter(pk=other.pk).update(title='新推荐标题')
        files._write(self.root / 'checked.json', {'at': 0, 'full_at': files.time.time()})
        files._write(self.root / 'attempt.json', {'at': 0})
        files.refresh_guide_snapshot(poll=True)
        self.assertContains(self.request(article=True), '新推荐标题')
        self.assertNotContains(self.request(article=True), '原推荐标题')
        self.assertEqual(files._load(self.root / 'index.json')['articles'][str(self.guide.pk)]['file'], old_body)
