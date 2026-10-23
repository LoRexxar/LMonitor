"""中央节点更新、退役和回归旧身份后，公开及私有历史引用仍可解析。"""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from botend.models import WowTalentNodeMetadata as Node, WowTalentVersion
from botend.services import spec_stats_snapshot as snapshots, spec_aggregation_cache as cache
from botend.services.simc_benchmark_result_snapshot import _write
from botend.services.stats_storage_codec import pack_detail, unpack_detail
from botend.services.stats_talent_references import _catalog


class TalentIdentityLifecycleTests(TestCase):
    def setUp(self):
        self.version = WowTalentVersion.objects.create(key='identity-history', branch='retail')
        self.row = Node.objects.create(talent_version=self.version, class_name='Mage', spec_name='Fire',
            node_id=100, talent_id=200, spell_id=300, name='原天赋',
            description='属于旧技能的完整说明。' * 40, description_zh='旧技能中文说明。' * 40)
        self.original_pk = self.row.pk

    def detail(self, row=None):
        row = row or self.row
        _, catalog = _catalog(row.class_name, row.spec_name, row.talent_version_id, ids=[row.pk])
        data = catalog[0][1]
        return {'dungeon_id': 1, 'sample_size': 78, 'talent_popularity_tree': {
            'metadata_version_id': row.talent_version_id, 'render_model': {
                'tree_set': {'class_name': row.class_name, 'spec_name': row.spec_name},
                'selection': {**data, 'points': 2, 'count': 78}}}}

    def test_publish_cache_and_rollback_survive_identity_replacement_and_delete(self):
        with tempfile.TemporaryDirectory() as temporary, self.settings(SPEC_AGGREGATION_WORK_ROOT=Path(temporary) / 'work'):
            path = Path(temporary) / 'published/1/Mage/Fire/dungeon.json'
            original = self.detail()
            snapshots.publish_projection(path, {'dungeons': [original]}, _write)
            old_index = path.read_bytes()
            cache.save_work(1, 'identity', 'v1', original)
            self.row.talent_id, self.row.spell_id = 201, 301
            self.row.description = '新技能说明'
            self.row.save(update_fields=['talent_id', 'spell_id', 'description'])
            self.assertNotEqual(self.row.pk, self.original_pk)
            previous = Node.all_objects.get(pk=self.original_pk)
            self.assertEqual((previous.talent_id, previous.spell_id, previous.is_current), (200, 300, False))
            with self.assertNumQueries(2):
                self.assertEqual(snapshots.read_projection(path, 'dungeon-1')['dungeons'], [original])
            self.assertEqual(cache.load_work(1, 'identity', 'v1'), original)
            replacement = self.detail()
            snapshots.publish_projection(path, {'dungeons': [replacement]}, _write)
            new_index = path.read_bytes()
            path.write_bytes(old_index)
            self.assertEqual(snapshots.read_projection(path, 'dungeon-1')['dungeons'], [original])
            path.write_bytes(new_index)
            Node.all_objects.filter(pk=self.original_pk).delete()
            self.row.delete()
            self.assertEqual(Node.objects.count(), 0)
            self.assertEqual(Node.all_objects.count(), 2)
            self.assertEqual(snapshots.read_projection(path, 'dungeon-1')['dungeons'], [replacement])
            self.assertEqual(cache.load_work(1, 'identity', 'v1'), original)
            with self.assertRaises(ProtectedError):
                self.version.delete()

    def test_talent_only_change_and_returning_identity_reuse_original_primary_key(self):
        original = self.detail()
        packed = pack_detail(original)
        self.row.talent_id = 201
        self.row.save(update_fields=['talent_id'])
        second_pk = self.row.pk
        self.assertNotEqual(second_pk, self.original_pk)
        self.assertEqual(unpack_detail(packed)['sample_size'], 78)
        self.row.talent_id = 200
        self.row.save(update_fields=['talent_id'])
        self.assertEqual(self.row.pk, self.original_pk)
        self.assertFalse(Node.all_objects.get(pk=second_pk).is_current)
        self.assertEqual(Node.objects.get().pk, self.original_pk)
        self.assertEqual(Node.all_objects.count(), 2)
        self.assertEqual(unpack_detail(packed), original)

    def test_partial_identity_repair_does_not_copy_old_skill_description(self):
        previous_text = self.row.description
        self.row.spell_id = 301
        self.row.save(update_fields=['spell_id'])
        self.assertEqual(Node.all_objects.get(pk=self.original_pk).description, previous_text)
        self.assertEqual(Node.objects.get(pk=self.row.pk).description, '')

    def test_bulk_repair_and_queryset_update_keep_previous_identities(self):
        original = self.detail()
        packed = pack_detail(original)
        self.row.talent_id = 201
        Node.objects.bulk_update([self.row], ['talent_id'])
        self.assertNotEqual(self.row.pk, self.original_pk)
        Node.objects.filter(pk=self.row.pk).update(spell_id=301)
        self.assertEqual(Node.all_objects.count(), 3)
        self.assertEqual(Node.objects.count(), 1)
        self.assertEqual(unpack_detail(packed), original)
        with self.assertRaises(ValueError):
            Node.objects.update(spell_id=F('spell_id') + 1)

    def test_identity_change_rolls_back_retirement_when_insert_fails(self):
        self.row.spell_id = 301
        from django.db.models import Model
        original_save = Model.save
        def fail_new(obj, *args, **kwargs):
            if isinstance(obj, Node) and kwargs.get('force_insert'):
                raise ValueError('模拟新身份写入失败')
            return original_save(obj, *args, **kwargs)
        with patch.object(Model, 'save', fail_new), self.assertRaises(ValueError):
            self.row.save(update_fields=['spell_id'])
        self.assertEqual(Node.objects.get().pk, self.original_pk)
        self.assertEqual(Node.objects.get().spell_id, 300)

    def test_bulk_import_can_restore_retired_identity_without_duplicate_rows(self):
        self.row.delete()
        incoming = Node(talent_version=self.version, class_name='Mage', spec_name='Fire',
                        node_id=100, talent_id=200, spell_id=300)
        Node.objects.bulk_create([incoming], ignore_conflicts=True)
        self.assertEqual(incoming.pk, self.original_pk)
        self.assertEqual(Node.objects.get().pk, self.original_pk)
        self.assertEqual(Node.all_objects.count(), 1)

    def test_retired_rows_are_not_rebound_to_another_identity_by_equal_text(self):
        original = self.detail()
        original['talent_popularity_tree']['render_model']['selection']['talent_id'] = 999
        packed = pack_detail(original)
        self.assertNotIn('_stats_talent_meta', json.dumps(packed))
        self.assertEqual(unpack_detail(packed), original)

    def test_publication_read_then_central_update_keeps_the_observed_identity(self):
        original = self.detail()
        def update_after_read(*args, **kwargs):
            catalog = _catalog(*args, **kwargs)
            Node.objects.filter(pk=self.original_pk).update(talent_id=201, spell_id=301)
            return catalog
        with patch('botend.services.stats_talent_references._catalog', side_effect=update_after_read):
            packed = pack_detail(original)
        self.assertEqual(unpack_detail(packed), original)
        self.assertFalse(Node.all_objects.get(pk=self.original_pk).is_current)

    def test_scope_change_does_not_rehome_old_reference(self):
        original = self.detail()
        packed = pack_detail(original)
        other = WowTalentVersion.objects.create(key='identity-other', branch='ptr')
        self.row.talent_version = other
        self.row.spec_name = 'Frost'
        self.row.save(update_fields=['talent_version', 'spec_name'])
        self.assertNotEqual(self.row.pk, self.original_pk)
        self.assertEqual(unpack_detail(packed), original)
        with self.assertRaises(ProtectedError):
            WowTalentVersion.objects.filter(pk=self.version.pk).delete()

    def test_same_identity_batch_keeps_bulk_text_update_and_rejects_bypass(self):
        self.row.description = '仅修正资料说明'
        with CaptureQueriesContext(connection) as queries:
            Node.objects.bulk_update([self.row], ['talent_id', 'description'])
        writes = [q['sql'] for q in queries if q['sql'].lstrip().startswith(('UPDATE', 'INSERT'))]
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0].startswith('UPDATE'))
        self.assertEqual(Node.all_objects.count(), 1)
        with self.assertRaises(ValueError):
            Node.all_objects.filter(pk=self.original_pk).update(id=self.original_pk + 10)
        with self.assertRaises(ValueError):
            Node.all_objects.filter(pk=self.original_pk)._raw_delete('default')


class TalentIdentityMigrationTests(TransactionTestCase):
    def test_reverse_migration_refuses_before_removing_identity_protection(self):
        from django.db.migrations.executor import MigrationExecutor
        from django.db.migrations.exceptions import IrreversibleError
        executor = MigrationExecutor(connection)
        with self.assertRaises(IrreversibleError):
            executor.migrate([('botend', '0253_character_bindings')])
        self.assertIn(('botend', '0254_preserve_talent_node_identity'),
                      MigrationExecutor(connection).loader.applied_migrations)
