"""验证原位更新、分支隔离、旧 build 展示、失败回滚和持久化补偿。"""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch, Mock

from django.test import TestCase
from django.utils import timezone
from botend.models import (SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot, WowTalentVersion,
                           WowTalentNodeMetadata, WowDataUpdateState, WowDataUpdateRun)
from botend.services import wow_data_update as update
from botend.services.wow_item_display import load_item_tooltip_metadata
from botend.wow.talents.default_versions import ensure_default_talent_versions
from botend.wow.talents.service import TalentBuildCodeService

OLD, NEW = '12.1.0.100', '12.1.0.101'


class UpdateSchedulingTests(TestCase):
    def setUp(self):
        from botend.models import MonitorTask
        MonitorTask.objects.update(is_active=False)
        self.task = update.get_monitor_task()
        self.task.is_active = True
        self.task.last_scan_time = timezone.now()
        self.task.save()

    def test_default_is_twelve_hours_and_normal_worker_never_claims_update(self):
        from botend.plugin_sync import claim_next_monitor_task, monitor_default_wait_time
        self.assertEqual(monitor_default_wait_time(update.TASK_NAME), 43200)
        self.assertEqual(self.task.wait_time, 43200)
        before = self.task.last_scan_time + timedelta(hours=11)
        after = self.task.last_scan_time + timedelta(hours=12, seconds=1)
        self.assertIsNone(claim_next_monitor_task(now=before, task_names=[update.TASK_NAME]))
        self.assertIsNone(claim_next_monitor_task(now=after))
        claimed = claim_next_monitor_task(now=after, task_names=[update.TASK_NAME])
        self.assertEqual(claimed.pk, self.task.pk)
        self.assertIsNone(claim_next_monitor_task(now=after, task_names=[update.TASK_NAME]))

    def test_normal_task_can_run_while_update_is_leased(self):
        from botend.models import MonitorTask
        from botend.plugin_sync import claim_next_monitor_task
        now = self.task.last_scan_time + timedelta(hours=13)
        normal = MonitorTask.objects.create(name='OtherMonitor', type=500, target='',
            is_active=True, wait_time=60, last_scan_time=now - timedelta(hours=1))
        claimed = claim_next_monitor_task(now=now, task_names=[update.TASK_NAME])
        self.assertEqual(claimed.pk, self.task.pk)
        self.assertEqual(claim_next_monitor_task(now=now).pk, normal.pk)

    def test_restart_preserves_administrator_interval(self):
        from botend.plugin_sync import sync_monitortasks_from_plugin_list
        from botend.controller.plugins.wow.WowDataVersionMonitor import WowDataVersionMonitor
        self.task.wait_time = 86400
        self.task.save(update_fields=['wait_time'])
        plugins = [None] * 37 + [WowDataVersionMonitor]
        sync_monitortasks_from_plugin_list(plugins, skip_indexes=set(range(37)))
        self.task.refresh_from_db()
        self.assertEqual(self.task.wait_time, 86400)

    def test_source_download_concurrency_is_bounded(self):
        from botend.services.journal_source import WagoJournalSource
        catalog = update.BranchCatalogSource('retail', NEW, Mock(), '.cache/unused-fixture')
        self.assertEqual(catalog.workers, 2)
        db2 = WagoJournalSource(NEW, '.cache/unused-fixture', max_workers=2)
        self.assertEqual(db2.max_workers, 2)

    def test_dedicated_worker_starts_without_blocking_caller(self):
        from threading import Event, current_thread
        from botend.plugin_sync import start_isolated_monitor_worker
        started, release = Event(), Event()
        received = {}
        def worker(*, task_names):
            received.update(names=task_names, thread=current_thread())
            started.set()
            release.wait(5)
        thread = start_isolated_monitor_worker(worker)
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(received['names'], {update.TASK_NAME})
            self.assertIsNot(received['thread'], current_thread())
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(2)


class IncrementalUpdateTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='增量测试', season_name='测试',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, game_build=OLD,
            gear_batch_key='stable', gear_sync_status='ready')
        self.item = WowItemSnapshot.objects.create(item_id=123, name='测试装备', name_zh='原装备',
            icon='inv_helmet_01', catalog_type='equipment', inventory_type=1,
            item_class_id=4, item_subclass_id=4, slot_key='head', allowable_class_mask=-1)
        self.variant = WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
            batch_key='stable', game_build=OLD, variant_key='raid-hero-1', variant_type='drop_equipment',
            item_level=300, upgrade_track='hero', compatible_slots=['head'], stats_json={'strength': 20})
        self.ptr = WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
            batch_key='stable', game_build=OLD, variant_key='ptr:raid-hero-1', variant_type='drop_equipment',
            item_level=300, upgrade_track='hero', compatible_slots=['head'], stats_json={'strength': 90},
            data_branch='ptr', metadata={'ptr_preview': True})
        self.talent, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'branch': 'retail', 'current_build': OLD, 'is_active': True, 'status': 'active',
            'is_default_simulator': True, 'label': '原天赋'})
        self.node = WowTalentNodeMetadata.objects.create(talent_version=self.talent,
            class_name='Warrior', spec_name='Fury', tree_type='spec', node_id=10, spell_id=20,
            name_zh='原天赋', description_zh='原描述', icon='spell_01', source='db2_backfill')
        self.source = Mock()
        self.source.discover.return_value = NEW
        self.source.prepare.side_effect = self.bundle
        self.stage = patch.object(update, 'prepare_talent_version', side_effect=self.candidate)
        self.stage.start()
        self.addCleanup(self.stage.stop)
        self.projections = patch.object(update, 'refresh_projections', side_effect=lambda state:
            WowDataUpdateState.objects.filter(pk=state.pk).update(projections_pending=False))
        self.projections.start()
        self.addCleanup(self.projections.stop)

    def bundle(self, branch, build, run, heartbeat):
        item = update.item_payload(self.item)
        item['name_zh'] = '新装备' if branch == 'retail' else '测试服装备'
        row = update.variant_payload(self.variant)
        row['metadata'] = {'data_branch': branch, 'ptr_preview': branch == 'ptr'}
        row['stats'] = {'strength': 30}
        if branch != 'retail':
            row['key'] = branch + ':' + row['key']
        item['variants'] = [row]
        return {'catalog': {'game_build': build, 'items': [item], 'rules': {}}, 'season_id': self.season.pk,
                'previous_batch': 'stable', 'dump_dir': '.cache/test', 'identities': [], 'activations': []}

    def candidate(self, bundle, branch, build, heartbeat):
        node = WowTalentNodeMetadata(class_name='Warrior', spec_name='Fury', tree_type='spec',
            node_id=10, spell_id=20, name_zh='新天赋', description_zh='新描述', icon='spell_02', source='db2_backfill')
        return SimpleNamespace(label='新天赋版本', major_version='12.1.0', current_build=build,
            source_dir=bundle['dump_dir'], granted_entries_json={'build': build, 'specs': {}}, _prepared_nodes=[node])

    def run_update(self, branches=('retail',)):
        return update.sync_game_data(branches=branches, source=self.source)

    def test_replaces_rows_in_place_and_keeps_other_branch(self):
        old_node_id, old_version_id = self.node.pk, self.talent.pk
        self.run_update()
        self.variant.refresh_from_db()
        self.ptr.refresh_from_db()
        self.node.refresh_from_db()
        self.talent.refresh_from_db()
        self.season.refresh_from_db()
        self.assertEqual((self.variant.stats_json, self.variant.game_build), ({'strength': 30}, NEW))
        self.assertEqual(self.ptr.stats_json, {'strength': 90})
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 2)
        self.assertEqual(self.season.gear_batch_key, 'stable')
        self.assertEqual((self.node.pk, self.node.description_zh), (old_node_id, '新描述'))
        self.assertEqual((self.talent.pk, self.talent.current_build), (old_version_id, NEW))
        self.assertFalse(WowTalentVersion.objects.filter(status='staging').exists())

    def test_unchanged_does_not_download_or_write_again(self):
        self.run_update()
        self.source.prepare.reset_mock()
        self.assertEqual(self.run_update()['retail']['status'], 'unchanged')
        self.source.prepare.assert_not_called()
        self.assertEqual(WowDataUpdateRun.objects.count(), 1)

    def test_incomplete_update_retains_all_current_data_and_retries(self):
        self.source.prepare.side_effect = ValueError('上游装备仍未更新')
        with self.assertRaisesMessage(ValueError, '上游装备仍未更新'):
            self.run_update()
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.game_build, OLD)
        self.assertEqual(WowDataUpdateState.objects.get(branch='retail').published_build, '')
        self.source.prepare.side_effect = self.bundle
        self.run_update()
        self.assertEqual(WowDataUpdateState.objects.get(branch='retail').published_build, NEW)

    def test_new_collection_fields_refresh_same_build_once(self):
        self.source.discover.return_value = {'build': NEW, 'catalog_build': NEW}
        self.run_update()
        self.source.prepare.reset_mock()
        self.source.discover.return_value = {'build': NEW, 'catalog_build': NEW,
                                             'data_schema': 'loot-specializations-1'}
        self.run_update()
        self.source.prepare.assert_called_once()
        self.assertEqual(WowItemSnapshot.objects.filter(item_id=123).count(), 1)
        self.source.prepare.reset_mock()
        self.assertEqual(self.run_update()['retail']['status'], 'unchanged')
        self.source.prepare.assert_not_called()

    def test_failure_after_equipment_write_rolls_back_equipment_and_talents(self):
        with patch.object(WowTalentVersion, 'save', side_effect=ValueError('发布失败')):
            with self.assertRaisesMessage(ValueError, '发布失败'):
                self.run_update()
        self.variant.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.variant.stats_json, {'strength': 20})
        self.assertEqual(self.item.name_zh, '原装备')
        self.assertEqual(WowDataUpdateState.objects.get(branch='retail').revision, 0)

    def test_ptr_does_not_overwrite_retail_fields_or_version(self):
        self.run_update(('ptr',))
        self.item.refresh_from_db()
        self.variant.refresh_from_db()
        self.ptr.refresh_from_db()
        self.assertEqual(self.item.name_zh, '原装备')
        self.assertEqual(self.item.metadata['branch_display']['ptr']['name_zh'], '测试服装备')
        self.assertEqual(self.variant.stats_json, {'strength': 20})
        self.assertEqual(self.ptr.stats_json, {'strength': 30})
        self.assertEqual(WowTalentVersion.objects.get(key='retail').current_build, OLD)

    def test_old_display_build_reads_current_branch_after_update(self):
        self.run_update()
        item = load_item_tooltip_metadata([{'item_id': 123, 'item_level': 300, 'game_build': OLD}])[0]
        self.assertEqual(item['stats_game_build'], NEW)
        self.assertEqual(item['variant_id'], self.variant.pk)

    def test_queue_failure_is_retried_without_republishing(self):
        with patch.object(update, 'refresh_projections', side_effect=OSError('队列暂不可用')):
            with self.assertRaisesMessage(ValueError, '队列暂不可用'):
                self.run_update()
        self.assertEqual(WowDataUpdateRun.objects.get().status, 'published')
        self.assertTrue(WowDataUpdateState.objects.get(branch='retail').projections_pending)
        self.source.prepare.reset_mock()
        self.run_update()
        self.source.prepare.assert_not_called()
        self.assertFalse(WowDataUpdateState.objects.get(branch='retail').projections_pending)

    def test_bootstrap_does_not_reset_current_version(self):
        self.run_update()
        ensure_default_talent_versions(WowTalentVersion)
        self.talent.refresh_from_db()
        self.assertEqual(self.talent.label, '新天赋版本')
        self.assertEqual(self.talent.current_build, NEW)

    def test_cache_key_changes_without_changing_stable_key(self):
        before = TalentBuildCodeService._version_cache_key(version_key='retail')
        self.run_update()
        after = TalentBuildCodeService._version_cache_key(version_key='retail')
        self.assertNotEqual(before, after)
        self.assertEqual(before[0], after[0])

    def test_sources_do_not_need_matching_build_numbers(self):
        actual, status = update.BranchCatalogSource._resolve_catalog_build(NEW, OLD)
        self.assertEqual(actual, OLD)
        self.assertEqual(status, 'branch_current')

    def test_missing_fields_preserve_current_values_and_retry(self):
        def partial(*args):
            bundle = self.bundle(*args)
            bundle['catalog']['items'][0]['variants'][0].update(stats={}, effects=[])
            return bundle
        self.source.prepare.side_effect = partial
        self.run_update()
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stats_json, {'strength': 20})
        self.assertEqual(WowDataUpdateState.objects.get(branch='retail').status, 'partial')
        self.source.prepare.side_effect = self.bundle
        self.run_update()
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stats_json, {'strength': 30})

    def test_old_build_does_not_hide_data_even_before_first_sync(self):
        result = load_item_tooltip_metadata([{'item_id': 123, 'item_level': 300, 'game_build': '1.0.0.1'}])[0]
        self.assertEqual(result['variant_id'], self.variant.pk)

    def test_loot_specialization_updates_are_scoped_to_branch(self):
        from botend.services.journal_loot import enrich_loot_specializations
        def with_specs(*args):
            bundle = self.bundle(*args)
            bundle['catalog']['items'][0]['loot_spec_ids'] = [65]
            return bundle
        self.source.prepare.side_effect = with_specs
        self.run_update(('ptr',))
        ptr = enrich_loot_specializations([{'item_id': 123}], 'ptr')[0]
        retail = enrich_loot_specializations([{'item_id': 123}], 'retail')[0]
        self.assertEqual(ptr['eligible_specs'], ['Paladin:Holy'])
        self.assertFalse(retail['eligible_specs'])

    def test_beta_is_separate_from_retail_and_ptr(self):
        self.run_update(('beta',))
        beta = WowItemVariantSnapshot.objects.get(data_branch='beta')
        self.assertEqual(beta.item_id, self.variant.item_id)
        self.assertNotIn(beta.pk, (self.variant.pk, self.ptr.pk))
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 3)

    def test_lost_lease_cannot_publish(self):
        from botend.models import MonitorTaskLeaseLost
        with patch.object(update, 'assert_lease', side_effect=MonitorTaskLeaseLost('租约失效')):
            with self.assertRaises(MonitorTaskLeaseLost):
                self.run_update()
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stats_json, {'strength': 20})

    def test_missing_talent_text_does_not_erase_existing_description(self):
        def missing(*args):
            version = self.candidate(*args)
            version._prepared_nodes[0].description_zh = ''
            return version
        with patch.object(update, 'prepare_talent_version', side_effect=missing):
            self.run_update()
        self.node.refresh_from_db()
        self.assertEqual(self.node.description_zh, '原描述')

    def test_cached_talent_statistics_read_current_text_by_id(self):
        from botend.services.wow_talent_display import refresh_aggregate_talents
        self.run_update()
        payload = {'talent_usage': {'nodes': [{'node_id': 10, 'tree_type': 'spec', 'name': '旧名称', 'count': 42}]},
                   'equipment': [{'node_id': 10, 'name': '装备名称'}]}
        refresh_aggregate_talents(payload, class_name='Warrior', spec_name='Fury')
        self.assertEqual(payload['talent_usage']['nodes'][0]['name'], '新天赋')
        self.assertEqual(payload['talent_usage']['nodes'][0]['count'], 42)
        self.assertEqual(payload['equipment'][0]['name'], '装备名称')

    def test_legacy_talent_key_resolves_to_current_branch(self):
        from botend.wow.talents.versioning import TalentVersionResolver
        legacy = WowTalentVersion.objects.create(key='retail-old', branch='retail',
            status='active', is_active=True, activated_at=timezone.now())
        self.assertEqual(TalentVersionResolver.get_active_by_key(legacy.key).pk, self.talent.pk)
        self.assertEqual(TalentVersionResolver.get_active_by_key('retail').pk, self.talent.pk)
        versions = [row for row in TalentVersionResolver.list_active() if row.branch == 'retail']
        self.assertEqual([row.pk for row in versions], [self.talent.pk])

    def test_omnium_same_id_never_uses_other_branch(self):
        from botend.services.simc_player_config import _enrich_omnium_talents
        ptr, _ = WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'branch': 'ptr', 'is_active': True, 'status': 'active'})
        for version, text in ((self.talent, '正式服节点'), (ptr, 'PTR 节点')):
            WowTalentNodeMetadata.objects.create(talent_version=version,
                tree_type='omnium', node_id=999999, name_zh=text)
        for is_ptr, expected in ((False, '正式服节点'), (True, 'PTR 节点')):
            detail = {'is_ptr': is_ptr, 'omnium_talents': [{'id': 999999, 'rank': 1}]}
            _enrich_omnium_talents(detail)
            self.assertEqual(detail['omnium_talents'][0]['display_name'], expected)


class TalentPreparationTests(TestCase):
    def test_staging_import_is_rolled_back_but_prepared_nodes_remain_available(self):
        # 真实 ORM 事务配合受控导入数据，验证候选永不变成构建历史副本。
        def import_fixture(command, **kwargs):
            if command == 'backfill_db2_talent_nodes':
                version = WowTalentVersion.objects.get(key=kwargs['version_key'])
                WowTalentNodeMetadata.objects.create(talent_version=version,
                    class_name='Warrior', spec_name='Fury', tree_type='spec', node_id=77,
                    name_zh='候选节点', description_zh='候选描述', icon='spell_01')
        before = (WowTalentVersion.objects.count(), WowTalentNodeMetadata.objects.count())
        with patch.object(update, 'call_command', side_effect=import_fixture), \
             patch.object(update, 'validate_talents') as validate:
            candidate = update.prepare_talent_version({'dump_dir': '.cache/unused-fixture'},
                'retail', NEW, Mock())
        validate.assert_called_once()
        self.assertEqual(candidate._prepared_nodes[0].node_id, 77)
        self.assertEqual((WowTalentVersion.objects.count(), WowTalentNodeMetadata.objects.count()), before)
        self.assertFalse(WowTalentVersion.objects.filter(key=candidate.key).exists())
