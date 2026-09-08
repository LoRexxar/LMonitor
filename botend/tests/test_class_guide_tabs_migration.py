"""纵向迁移保留人工保护、来源信息与非选项卡正文。"""
import importlib
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from botend.guide_models import ClassGuide
from botend.services.class_guide_service import has_manual_content, save_article


class GuideTabsMigrationTests(TestCase):
    def test_migration_preserves_manual_protection_and_is_idempotent(self):
        migration = importlib.import_module('botend.migrations.0221_expand_class_guide_tabs')
        old = '## 方案\n:::tabs\n:::tab 单体\n人工注释 [[spell:30451]]\n:::\n:::tab 群体\n正文\n:::\n:::\n'
        records = []
        for manual in [False, True]:
            guide = ClassGuide.objects.create(title='标题', slug='tabs-' + str(manual),
                game_version='12.1', class_name='Mage', spec_name='Arcane', spec_id=62,
                content_markdown=old, source_markdown=old, source_modified='2026-09-08',
                source_payload={'原文': '保留'}, source_hash='a' * 64,
                imported_content_hash='manual-baseline' if manual else migration.content_hash('标题', old))
            records.append((guide, manual))
        migration.expand_articles(apps, SimpleNamespace(connection=connection))
        for guide, manual in records:
            guide.refresh_from_db()
            self.assertEqual(has_manual_content(guide), manual)
            self.assertNotIn(':::tab', guide.content_markdown)
            self.assertEqual(guide.content_markdown, guide.source_markdown)
            self.assertIn('人工注释 [[spell:30451]]', guide.content_markdown)
            self.assertEqual(guide.source_payload, {'原文': '保留'})
            self.assertEqual(guide.source_modified, '2026-09-08')
            self.assertEqual(guide.source_hash, 'a' * 64)
        stamp = records[0][0].updated_at
        migration.expand_articles(apps, SimpleNamespace(connection=connection))
        records[0][0].refresh_from_db()
        self.assertEqual(records[0][0].updated_at, stamp)
        guide = records[0][0]
        saved = save_article(guide.pk, guide.title, content_markdown=old, source_markdown=old,
                             expected_updated_at=guide.updated_at, imported=True)
        self.assertNotIn(':::tab', saved.content_markdown)
        self.assertNotIn(':::tab', saved.source_markdown)
        self.assertFalse(has_manual_content(saved))
