"""使用最终完整包验证真实导入和现有职业攻略阅读接口。"""

import io
import uuid
from time import perf_counter
from pathlib import Path
from unittest import skipUnless

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.template.loader import render_to_string

from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.guide_models import ClassGuide
from botend.services.class_guide_service import save_article
from botend.services.class_guide_snapshot import read_guide_page, read_guide_snapshot
from botend.services.class_guide_service import check_article
from botend.services.class_guide_render import render_blocks


FULL_BUNDLE = Path(__file__).resolve().parents[1] / 'data/class_guides/icy-full-initial-20261022.zip'


@skipUnless(FULL_BUNDLE.exists(), '完整包生成后执行真实数据导入验证')
@override_settings(SECURE_SSL_REDIRECT=False)
class IcyFullBundleImportTests(TestCase):
    def setUp(self):
        override = override_settings(CLASS_GUIDE_SNAPSHOT_ROOT=Path('tmp/icy-full-snapshots') / uuid.uuid4().hex)
        override.enable()
        self.addCleanup(override.disable)

    def test_complete_bundle_import_reading_and_manual_protection(self):
        output = io.StringIO()
        call_command('import_class_guide_drafts', input_zip=str(FULL_BUNDLE), dry_run=True, stdout=output)
        self.assertFalse(ClassGuide.objects.exists())
        call_command('import_class_guide_drafts', input_zip=str(FULL_BUNDLE), stdout=output)
        guides = ClassGuide.objects.filter(slug__startswith='icy-')
        self.assertEqual(set(guides.values_list('spec_id', flat=True)), set(SPEC_IDENTITY_MAP))
        self.assertEqual(guides.filter(tags__name='icy').count(), 40)
        self.assertEqual(guides.filter(tags__name='导读').count(), 0)
        for guide in guides:
            self.assertEqual(len(guide.source_payload['chapters']), 10)
            self.assertGreater(len(guide.content_markdown), 10000)
            self.assertIn('仅供非商业阅读', guide.content_markdown)
            self.assertIn('## 技能循环与冷却', guide.content_markdown)
            self.assertIn('## 装备选择', guide.content_markdown)
        call_command('refresh_class_guide_snapshots', stdout=output)
        ids = list(guides.values_list('id', flat=True))
        started = perf_counter()
        with self.assertNumQueries(0):
            for guide_id in ids:
                self.assertTrue(read_guide_page(guide_id)[0])
        print(f'完整包文件读取：{len(ids)} 篇，0 次 SQL，{(perf_counter() - started) * 1000:.1f} 毫秒（本地 SQLite 夹具）')
        longest = max(guides, key=lambda guide: len(guide.content_markdown))
        checks = check_article(longest)
        expected = render_to_string('shared/class_guide_blocks.html', {
            'blocks': render_blocks(longest.blocks, checks['references'], longest),
        })
        self.assertEqual(read_guide_snapshot(longest.pk)['article_html'], expected)
        self.assertEqual(self.client.get('/portal/class-guides/').status_code, 200)
        for guide in guides:
            response = self.client.get(f'/portal/class-guides/{guide.id}/')
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, guide.title)
            self.assertContains(response, 'Icy Veins')
        before = list(guides.order_by('id').values_list('id', 'updated_at'))
        call_command('import_class_guide_drafts', input_zip=str(FULL_BUNDLE), stdout=output)
        self.assertEqual(before, list(guides.order_by('id').values_list('id', 'updated_at')))
        selected = guides.get(spec_id=62)
        save_article(selected.id, '保留人工修订', content_markdown='管理员已核对并修改的正文。',
                     expected_updated_at=selected.updated_at)
        call_command('import_class_guide_drafts', input_zip=str(FULL_BUNDLE), stdout=output)
        selected.refresh_from_db()
        self.assertEqual(selected.content_markdown, '管理员已核对并修改的正文。')
        self.assertEqual(ClassGuide.objects.count(), 40)
