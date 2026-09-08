"""验证简化文章时按原展示逻辑迁移，并保留正文、来源及人工修改。"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class GuideArticleMigrationTests(TransactionTestCase):
    before = [('botend', '0218_consolidate_class_guide_monitor')]
    after = [('botend', '0219_simplify_class_guide_articles')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.apps = executor.loader.project_state(self.before).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_migration_keeps_displayed_manual_article_and_drops_history(self):
        Guide = self.apps.get_model('botend', 'ClassGuide')
        Revision = self.apps.get_model('botend', 'ClassGuideRevision')
        guide = Guide.objects.create(title='人工标题', slug='migration-manual', class_name='Mage',
            spec_name='Arcane', spec_id=62, game_version='12.1', source_url='https://maxroll.gg/wow/class-guides/example',
            author_profile={'name': '自定义作者'}, revision_number=3)
        old = Revision.objects.create(guide=guide, number=1, origin='translation', title='初次导入',
            content_markdown='早期正文')
        Revision.objects.create(guide=guide, number=2, origin='manual', title='人工标题',
            content_markdown='## 人工正文\n保留技能 [[spell:30451]]', source_markdown='## Source',
            source_modified='2026-09-07', source_hash='a' * 64, source_payload={'featuredImage': 'https://example.com/image.png'},
            audit={'untranslated': [], 'source_refs': {}, 'publishable': True})
        Revision.objects.create(guide=guide, number=3, origin='translation', title='未采用的新来源',
            content_markdown='不应覆盖人工内容', audit={'manual_conflict': True})
        guide.published_revision = old; guide.save(update_fields=['published_revision'])
        executor = MigrationExecutor(connection); executor.migrate(self.after)
        apps = executor.loader.project_state(self.after).apps
        current = apps.get_model('botend', 'ClassGuide').objects.get(pk=guide.pk)
        self.assertEqual(current.title, '人工标题')
        self.assertIn('保留技能 [[spell:30451]]', current.content_markdown)
        self.assertEqual(current.source_modified, '2026-09-07')
        self.assertEqual(current.source_hash, 'a' * 64)
        self.assertEqual(current.author_profile, {'name': '自定义作者'})
        self.assertEqual(current.imported_content_hash, '')
        self.assertNotIn('publishable', current.check_data)
        self.assertNotIn(Revision._meta.db_table, connection.introspection.table_names())
        self.assertFalse(hasattr(current, 'revision_number'))

    def test_migration_keeps_latest_automatic_content_eligible_for_sync(self):
        Guide = self.apps.get_model('botend', 'ClassGuide')
        Revision = self.apps.get_model('botend', 'ClassGuideRevision')
        guide = Guide.objects.create(title='旧标题', slug='migration-auto', class_name='Mage',
            spec_name='Arcane', spec_id=62, game_version='12.1', revision_number=2)
        Revision.objects.create(guide=guide, number=1, origin='translation', title='旧标题', content_markdown='旧正文')
        Revision.objects.create(guide=guide, number=2, origin='translation', title='当前标题', content_markdown='当前正文')
        executor = MigrationExecutor(connection); executor.migrate(self.after)
        apps = executor.loader.project_state(self.after).apps
        current = apps.get_model('botend', 'ClassGuide').objects.get(pk=guide.pk)
        from botend.services.class_guide_service import has_manual_content
        self.assertEqual(current.content_markdown, '当前正文')
        self.assertFalse(has_manual_content(current))
