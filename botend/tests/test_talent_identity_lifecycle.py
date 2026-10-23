"""当前资料引用、普通更新与0254向前清理的契约。"""
import importlib
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import connection, connections, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, SimpleTestCase
from django.test.utils import CaptureQueriesContext

from botend.models import WowTalentNodeMetadata as Node, WowTalentVersion
from botend.services.stats_storage_codec import pack_detail, unpack_detail
from botend.services.stats_talent_references import _catalog, StaleTalentReferences
from botend.services import spec_stats_snapshot as snapshot, spec_aggregation_cache as cache
from botend.services.simc_benchmark_result_snapshot import _write


class TalentIdentityLifecycleTests(TestCase):
    def setUp(self):
        self.version = WowTalentVersion.objects.create(key='current-reference-test', branch='retail')
        self.row = Node.objects.create(talent_version=self.version, class_name='Mage', spec_name='Fire',
            tree_type='spec', node_id=100, talent_id=200, spell_id=300, name='当前天赋',
            description='中央资料说明。' * 40)

    def detail(self):
        _, catalog = _catalog('Mage', 'Fire', self.version.pk)
        return {'dungeon_id': 1, 'sample_size': 78, 'talent_popularity_tree': {
            'metadata_version_id': self.version.pk, 'render_model': {
                'tree_set': {'class_name': 'Mage', 'spec_name': 'Fire'},
                'selection': {**catalog[0][1], 'points': 2, 'count': 78}}}}

    def test_logical_reference_survives_row_replacement_and_legacy_primary_key(self):
        detail = self.detail()
        packed = pack_detail(detail)
        self.assertEqual(packed['_stats_storage']['metadata']['schema'], 2)
        legacy = deepcopy(packed)
        legacy['_stats_storage']['metadata']['schema'] = 1
        legacy['_stats_storage']['metadata']['rows'][0][0] = self.row.pk
        data = {field.attname: getattr(self.row, field.attname) for field in Node._meta.concrete_fields
                if not field.primary_key}
        self.row.delete()
        replacement = Node.objects.create(**data)
        self.assertEqual(Node.all_objects.filter(talent_version=self.version).count(), 1)
        with self.assertNumQueries(2):
            self.assertEqual(unpack_detail(packed), detail)
        self.assertEqual(unpack_detail(legacy), detail)
        replacement.description = '最新说明'
        replacement.save(update_fields=['description'])
        self.assertEqual(unpack_detail(packed)['talent_popularity_tree']['render_model']['selection']['description'], '最新说明')

    def test_identity_change_updates_in_place_and_invalidates_public_and_private_outputs(self):
        with TemporaryDirectory() as temporary, self.settings(SPEC_AGGREGATION_WORK_ROOT=Path(temporary) / 'work'):
            path = Path(temporary) / '1/Mage/Fire/dungeon.json'
            old = self.detail()
            snapshot.publish_projection(path, {'dungeons': [old]}, _write)
            cache.save_work(1, 'test', 'same', old)
            pk = self.row.pk
            for spell in [301, 302, 300]:
                self.row.spell_id = spell
                self.row.save(update_fields=['spell_id'])
                self.assertEqual(self.row.pk, pk)
                self.assertEqual(Node.all_objects.filter(talent_version=self.version).count(), 1)
                if spell != 300:
                    self.assertTrue(snapshot.read_projection(path, 'dungeon-1')['_stats_refresh_required'])
                    self.assertIsNone(cache.load_work(1, 'test', 'same'))
            new = self.detail()
            snapshot.publish_projection(path, {'dungeons': [new]}, _write)
            self.assertEqual(snapshot.read_projection(path, 'dungeon-1')['dungeons'], [new])

    def test_bulk_updates_and_deletes_do_not_create_retired_rows(self):
        pk = self.row.pk
        Node.objects.filter(pk=pk).update(talent_id=999)
        self.row.refresh_from_db()
        self.row.spell_id = 444
        Node.objects.bulk_update([self.row], ['spell_id'])
        self.assertEqual(Node.all_objects.filter(talent_version=self.version).count(), 1)
        Node.objects.filter(pk=pk).delete()
        self.assertFalse(Node.all_objects.filter(pk=pk).exists())
        Node.objects.create(talent_version=self.version, node_id=10, spell_id=20)
        self.version.delete()
        self.assertFalse(Node.all_objects.filter(talent_version_id=self.version.pk).exists())

    def test_publication_guard_rejects_structure_change_and_preserves_index(self):
        from botend.services.stats_talent_dependencies import structure_token, publication_metadata_guard
        self.version.key, self.version.status, self.version.is_active = 'retail', 'active', True
        WowTalentVersion.objects.exclude(pk=self.version.pk).filter(key='retail').delete()
        self.version.save()
        token = structure_token('Mage')
        Node.objects.filter(pk=self.row.pk).update(spell_id=301)
        with self.assertRaisesMessage(ValueError, '聚合期间'):
            with publication_metadata_guard('Mage', token):
                self.fail('变化后不得进入发布')
        with publication_metadata_guard('Mage', structure_token('Mage')):
            pass


    def test_readonly_check_command_and_page_report_pending_then_accept_rebuild(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.test import RequestFactory
        from botend.models import SeasonMeta
        from botend.portal.spec_detail_views import SpecDetailDungeonView, SpecDetailRaidView
        from botend.services.stats_talent_dependencies import structure_token
        from botend.services.spec_stats_retention import maintain_scope
        with TemporaryDirectory() as temporary, self.settings(MEDIA_ROOT=temporary):
            season = SeasonMeta.objects.create(season_key='current-check', season_name='测试',
                mplus_zone_id=1, raid_zone_id=1, mplus_encounters=[{'id': 1, 'name': 'One'}],
                raid_encounters=[{'id': 1, 'name': 'Boss'}])
            root = Path(temporary) / 'aggregated' / str(season.pk) / 'Mage/Fire'
            def publish():
                detail = self.detail()
                detail['boss_id'] = 1
                token = structure_token('Mage')
                snapshot.publish_projection(root / 'dungeon.json', {
                    'dungeons': [detail], 'summary': {**detail, 'dungeon_id': 'all'},
                    '_talent_structure_token': token}, _write)
                snapshot.publish_projection(root / 'raid.json', {
                    'difficulties': [{'difficulty': difficulty, 'zone_groups': [{'bosses': [detail]}]}
                                     for difficulty in (4, 5)], '_talent_structure_token': token}, _write)
            def check():
                call_command('check_talent_aggregation', season=season.pk, class_name='Mage',
                    spec_name='Fire', require_current=True, stdout=StringIO())
            publish()
            check()
            Node.objects.filter(pk=self.row.pk).update(spell_id=301)
            with self.assertRaises(CommandError):
                check()
            with patch('botend.portal.spec_detail_views._base_context', return_value={'season': season}):
                factory = RequestFactory()
                for view, query in [(SpecDetailDungeonView, '?dungeon_id=1'), (SpecDetailRaidView, '?boss_id=1')]:
                    response = view.as_view()(factory.get('/' + query), class_name='Mage', spec_name='Fire')
                    self.assertEqual(response.status_code, 503)
                    self.assertIn('统计正在重新生成', response.content.decode())
            # 回收只校验文件完整性，不展开已经过期的数据库引用。
            with self.assertNumQueries(0):
                self.assertEqual(maintain_scope(root, dry_run=True)['status'], 'ok')
            publish()
            with CaptureQueriesContext(connection) as queries:
                check()
            self.assertFalse(any(q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for q in queries))
            self.assertEqual(snapshot.read_projection(root / 'dungeon.json', 'dungeon-1')['dungeons'][0]['sample_size'], 78)


class CurrentTalentMigrationTests(SimpleTestCase):
    def test_upgrade_from_0254_prunes_in_batches_and_preserves_current_and_names(self):
        # 独立内存数据库，真实执行到0254再升级，不逆向篡改已发布迁移。
        alias = 'talent_migration_acceptance'
        config = dict(connections['default'].settings_dict)
        config.update(NAME=':memory:')
        previous_databases = type(self).databases
        type(self).databases = frozenset({*previous_databases, alias})
        connections.databases[alias] = config
        db = connections[alias]
        try:
            executor = MigrationExecutor(db)
            apps = executor.loader.project_state([('botend', '0254_preserve_talent_node_identity')]).apps
            Version = apps.get_model('botend', 'WowTalentVersion')
            Historical = apps.get_model('botend', 'WowTalentNodeMetadata')
            # 按0254真实模型状态创建这两张表；更早的数据迁移含硬编码default，不在独立库重放。
            with db.schema_editor() as editor:
                editor.create_model(Version)
                editor.create_model(Historical)
            for app, name in executor.loader.graph.forwards_plan(('botend', '0254_preserve_talent_node_identity')):
                executor.recorder.record_applied(app, name)
            version = Version.objects.using(alias).create(key='migration-test')
            current = Historical.all_objects.using(alias).create(talent_version_id=version.pk,
                class_name='Mage', spec_name='Fire', node_id=100, spell_id=300, talent_id=200)
            Historical.all_objects.using(alias).bulk_create([
                Historical(talent_version_id=version.pk, node_id=i, spell_id=i,
                           talent_id=i, is_current=False) for i in range(1000, 1510)])
            name = Historical.all_objects.using(alias).create(talent_version_id=version.pk,
                localization_only=True, is_current=False, name='保留名称', reference_id=999)
            migration = importlib.import_module('botend.migrations.0255_use_current_talent_metadata')
            conflict = Historical.all_objects.using(alias).create(talent_version_id=version.pk,
                class_name='Mage', spec_name='Fire', node_id=100, spell_id=300, talent_id=201)
            with self.assertRaisesMessage(RuntimeError, '0255未清理任何节点'):
                migration.discard_retired_nodes(apps, type('Editor', (), {'connection': db})())
            self.assertEqual(Historical.all_objects.using(alias).filter(localization_only=False, is_current=False).count(), 510)
            conflict.delete(using=alias)
            with CaptureQueriesContext(db) as queries:
                MigrationExecutor(db).migrate([('botend', '0255_use_current_talent_metadata')])
            rows = Historical.all_objects.using(alias)
            self.assertTrue(rows.filter(pk=current.pk).exists())
            self.assertTrue(rows.filter(pk=name.pk).exists())
            self.assertFalse(rows.filter(localization_only=False, is_current=False).exists())
            deletes = [q['sql'] for q in queries if q['sql'].startswith('DELETE') and 'wow_talent_node_metadata' in q['sql']]
            self.assertEqual(len(deletes), 2)
            migration = importlib.import_module('botend.migrations.0255_use_current_talent_metadata')
            with CaptureQueriesContext(db) as repeated:
                migration.discard_retired_nodes(apps, type('Editor', (), {'connection': db})())
            self.assertFalse(any(q['sql'].startswith('DELETE') for q in repeated))
            with self.assertRaises(IntegrityError), transaction.atomic(using=alias):
                rows.create(talent_version_id=version.pk, class_name='Mage', spec_name='Fire',
                            node_id=100, spell_id=300, talent_id=201)
        finally:
            db.close()
            del connections[alias]
            connections.databases.pop(alias, None)
            type(self).databases = previous_databases
