"""名称迁移必须复用原记录，保留版本，并可恢复旧结构。"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LocalizationMigrationTests(TransactionTestCase):
    before = [('botend', '0219_simplify_class_guide_articles')]
    after = [('botend', '0220_consolidate_wow_localization')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.apps = executor.loader.project_state(self.before).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_moves_all_kinds_to_the_existing_talent_table_and_can_reverse(self):
        old = self.apps.get_model('botend', 'ClassGuideTerm')
        version = self.apps.get_model('botend', 'WowTalentVersion').objects.create(key='12.1', major_version='12.1')
        node = self.apps.get_model('botend', 'WowTalentNodeMetadata').objects.create(talent_version=version,
            talent_id=900, node_id=800, name='Arcane Blast', name_zh='原表中文')
        for kind, identity, english in [('talent', 777, 'Arcane Blast'), ('spell', 30451, 'Arcane Blast'),
                ('item', 123, 'Test Item'), ('phrase', 111, 'Boss Name'), ('macro', 222, 'Corruption')]:
            old.objects.create(game_version='12.1', kind=kind, object_id=identity, name_en=english,
                name_zh='名称中文', evidence='官方名称')
        executor = MigrationExecutor(connection); executor.migrate(self.after)
        apps = executor.loader.project_state(self.after).apps
        talents = apps.get_model('botend', 'WowTalentNodeMetadata')
        current = talents.objects.get(pk=node.pk)
        self.assertEqual(current.name_zh, '原表中文')
        self.assertEqual(current.reference_aliases, [777])
        self.assertEqual(talents.objects.filter(localization_only=True).count(), 4)
        self.assertEqual(talents.objects.get(name_kind='spell', reference_id=30451).name_zh, '名称中文')
        self.assertFalse(apps.get_model('botend', 'WowSpellSnapshot').objects.exists())
        self.assertEqual(talents.objects.get(name_kind='item', reference_id=123).name_zh, '名称中文')
        self.assertFalse(apps.get_model('botend', 'WowItemSnapshot').objects.exists())
        self.assertNotIn(old._meta.db_table, connection.introspection.table_names())
        executor = MigrationExecutor(connection); executor.migrate(self.before)
        restored = executor.loader.project_state(self.before).apps
        self.assertTrue(restored.get_model('botend', 'ClassGuideTerm').objects.filter(kind='item', object_id=123).exists())
        self.assertEqual(restored.get_model('botend', 'WowTalentNodeMetadata').objects.get(pk=node.pk).name_zh, '原表中文')
