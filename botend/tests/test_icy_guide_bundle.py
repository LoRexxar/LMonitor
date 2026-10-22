"""验证交付的 Icy 导读包能够沿用现有导入和前台阅读流程。"""

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

from django.core.management import call_command
from django.test import TestCase, override_settings

from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.guide_models import ClassGuide
from botend.services.class_guide_service import save_article


BUNDLE = Path(__file__).resolve().parents[1] / 'data/class_guides/icy-initial-digests-20261022.zip'


@override_settings(SECURE_SSL_REDIRECT=False)
class IcyGuideBundleTests(TestCase):
    def test_bundle_import_reading_idempotency_and_manual_preservation(self):
        """全包真实导入、公开阅读、重复导入及人工稿保护。"""
        output = io.StringIO()
        call_command('import_class_guide_drafts', input_zip=str(BUNDLE), dry_run=True, stdout=output)
        self.assertEqual(ClassGuide.objects.count(), 0)
        self.assertIn('40 篇草稿', output.getvalue())
        original = ClassGuide.objects.create(
            slug='arcane-mage-raid-guide', game_version='12.1.5', title='已有攻略',
            spec_id=62, class_name='Mage', spec_name='Arcane',
            source_url='https://maxroll.gg/wow/class-guides/arcane-mage-raid-guide',
            content_markdown='原有正文保持不变。')
        call_command('import_class_guide_drafts', input_zip=str(BUNDLE), stdout=output)
        guides = ClassGuide.objects.filter(slug__startswith='icy-')
        self.assertEqual(guides.count(), 40)
        self.assertEqual(set(guides.values_list('spec_id', flat=True)), set(SPEC_IDENTITY_MAP))
        self.assertEqual(guides.filter(tags__name='icy').count(), 40)
        self.assertEqual(guides.filter(tags__name='maxroll').count(), 0)
        self.assertEqual(guides.filter(archived=True).count(), 0)
        original.refresh_from_db()
        self.assertEqual(original.content_markdown, '原有正文保持不变。')
        catalog = self.client.get('/portal/class-guides/')
        self.assertEqual(catalog.status_code, 200)
        for guide in guides:
            with self.subTest(spec_id=guide.spec_id):
                response = self.client.get(f'/portal/class-guides/{guide.id}/')
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, guide.title)
                self.assertContains(response, '中文简要导读与原文导航')
                for chapter in guide.source_payload['chapters']:
                    self.assertContains(response, chapter['url'])
        before = list(guides.order_by('id').values_list('id', 'updated_at'))
        call_command('import_class_guide_drafts', input_zip=str(BUNDLE), stdout=output)
        self.assertEqual(before, list(guides.order_by('id').values_list('id', 'updated_at')))
        selected = guides.get(spec_id=62)
        save_article(selected.id, '人工编辑保留', content_markdown='管理员已修改正文。',
                     expected_updated_at=selected.updated_at)
        call_command('import_class_guide_drafts', input_zip=str(BUNDLE), stdout=output)
        selected.refresh_from_db()
        self.assertEqual(selected.title, '人工编辑保留')
        self.assertEqual(selected.content_markdown, '管理员已修改正文。')
        self.assertEqual(ClassGuide.objects.count(), 41)

    def test_package_sources_and_chapters_match_delivered_index(self):
        """交付文件的元数据、目录、正文校验值与来源一致。"""
        with ZipFile(BUNDLE) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            index = json.loads(archive.read('source-index.json'))
            self.assertEqual(manifest['total'], len(SPEC_IDENTITY_MAP))
            self.assertEqual(len(manifest['records']), len(SPEC_IDENTITY_MAP))
            sources = {row['spec_id']: row for row in index['records']}
            for entry in manifest['records']:
                draft = json.loads(archive.read(entry['slug'] + '-' + entry['game_version'] + '.json'))
                source = sources[draft['spec_id']]
                self.assertEqual(draft['source_url'], source['source_url'])
                self.assertEqual(draft['source_modified'], source['source_modified'])
                self.assertEqual(draft['author'], source['author'])
                self.assertEqual(draft['game_version'], source['game_version'])
                self.assertEqual(draft['source_payload']['chapters'], source['chapters'])
                self.assertEqual(hashlib.sha256(draft['content_markdown'].encode()).hexdigest(), entry['markdown_sha256'])
                self.assertEqual(len(source['chapters']), len({c['url'] for c in source['chapters']}))
                self.assertGreaterEqual(len(source['chapters']), 4)
