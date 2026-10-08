"""手册统一读模型、完整角色树和公开零数据库读取验证。"""
from copy import deepcopy
from io import StringIO
import json
import subprocess
from unittest.mock import patch

from django.core.management import call_command
from django.db import transaction
from django.http import Http404
from django.test import RequestFactory, TestCase

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.portal.adventure_journal import (catalog_data, detail_data, PortalAdventureJournalAPIView,
                                          PortalAdventureJournalTooltipView, PortalAdventureJournalArtView)
from botend.services import journal_snapshot as files
from botend.services.journal_service import compile_journal
from botend.tests.test_adventure_journal import fixture
from botend.tests.journal_snapshot_fixtures import isolate_journal_snapshots, warm_journal


class JournalSnapshotTests(TestCase):
    def setUp(self):
        self.root = isolate_journal_snapshots(self)
        self.factory = RequestFactory()
        rows, catalog, report = compile_journal(fixture())
        payload = deepcopy(rows[0])
        bosses = payload.pop('encounters')
        self.release = JournalRelease.objects.create(build='12.1.0.69587', status='completed',
                                                    manifest={'catalog': catalog}, report=report)
        self.state = JournalState.objects.create(active_release=self.release)
        self.instance = JournalInstance.objects.create(release=self.release, journal_id=10, name=payload['name'],
                                                      kind=payload['kind'], expansion=payload['expansion'], payload=payload)
        for boss in bosses:
            self.boss = JournalEncounter.objects.create(instance=self.instance, journal_id=boss['id'], name=boss['name'], payload=boss)

    def test_public_cold_reads_never_query_or_build_and_only_published_instances_return_404(self):
        with self.assertNumQueries(0), patch.object(files, '_write') as write:
            self.assertIsNone(catalog_data(self.factory.get('/'))['release'])
            with self.assertRaises(files.JournalSnapshotUnavailable):
                detail_data(self.factory.get('/'), 10)
            response = PortalAdventureJournalTooltipView.as_view()(self.factory.get('/'), 10, 'spell', 100)
        self.assertEqual(response.status_code, 503)
        write.assert_not_called()
        files.refresh_journal_snapshot()
        with self.assertNumQueries(0), self.assertRaises(Http404):
            detail_data(self.factory.get('/'), 9999)

    def test_catalog_detail_spell_and_status_are_zero_sql_and_same_source(self):
        warm_journal()
        with self.assertNumQueries(0), patch.object(files, '_write') as write:
            catalog = catalog_data(self.factory.get('/', {'q': '测试首领'}))
            detail = detail_data(self.factory.get('/', {'difficulty': 2, 'role': 'healer'}), 10)
            spell = PortalAdventureJournalTooltipView.as_view()(self.factory.get('/', {'difficulty': 2}), 10, 'spell', 100)
            status = PortalAdventureJournalAPIView.as_view()(self.factory.get('/', {'snapshot_status': 1}), 10)
        self.assertEqual(catalog['instances'][0]['source'], detail['source'])
        self.assertEqual(detail['boss']['roles'][0]['title'], '治疗者')
        self.assertEqual(json.loads(spell.content)['name'], '英雄技能')
        self.assertEqual(json.loads(status.content)['snapshot']['state'], 'ready')
        write.assert_not_called()

    def test_loot_html_only_reads_menu_and_skill_html_only_reads_selected_boss(self):
        warm_journal()
        with patch.object(files, '_content', wraps=files._content) as read, self.assertNumQueries(0):
            loot = detail_data(self.factory.get('/'), 10, for_html=True)
        self.assertEqual(read.call_count, 1)
        self.assertNotIn('skills', loot['boss'])
        with patch.object(files, '_content', wraps=files._content) as read, self.assertNumQueries(0):
            guide = detail_data(self.factory.get('/', {'tab': 'skills', 'difficulty': 2}), 10, for_html=True)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(guide['boss']['skill_total'], 2)
        self.assertTrue(guide['skill_filter_data'])

    def test_art_and_item_reference_validation_do_not_load_journal_models(self):
        warm_journal()
        with self.assertNumQueries(0), self.assertRaises(Http404):
            PortalAdventureJournalArtView.as_view()(self.factory.get('/'), 99999)
        with self.assertNumQueries(0), self.assertRaises(Http404):
            PortalAdventureJournalTooltipView.as_view()(self.factory.get('/'), 10, 'item', 99999)
        with patch('botend.services.journal_tooltip.tooltip', return_value={'name': '物品详情'}), self.assertNumQueries(0):
            result = PortalAdventureJournalTooltipView.as_view()(self.factory.get('/'), 10, 'item', 60)
        self.assertEqual(json.loads(result.content)['name'], '物品详情')

    def test_role_trees_and_catalog_filters_match_browser_and_do_not_mutate_files(self):
        files.refresh_journal_snapshot()
        index = files.read_index()
        instance = files.read_instance(index, 10)
        skills = files.read_skills(index, instance, instance['bosses'][0], 2)
        # 父节点职责不匹配时，匹配的子节点必须成为根节点。
        skills = deepcopy(skills)
        skills['sections'][0]['roles'] = ['tank']
        skills['sections'][1]['roles'] = ['healer']
        skills['sections'][1]['has_dynamic'] = True
        original = deepcopy(skills)
        roles = ['', 'tank', 'healer', 'dps', 'invalid']
        filters = [{'q': q, 'kind': kind, 'tier': tier} for q in ('', '测试首领', '不存在')
                   for kind in ('', 'dungeon', 'raid') for tier in ('', '70', '999')]
        catalog = catalog_data(self.factory.get('/'))['catalog_filter_data']
        expected_catalog = [[row['id'] for row in catalog['rows'] if (
            (not selected['kind'] or row['kind'] == selected['kind']) and
            (not selected['tier'] or int(selected['tier']) in row['tier_ids']) and
            (not selected['q'] or any(selected['q'].casefold() in name.casefold() for name in row['search_names'])))]
            for selected in filters]
        expected_skills = [files.filter_skills(skills, role) for role in roles]
        script = """const fs=require('fs'),vm=require('vm');
        vm.runInThisContext(fs.readFileSync('static/portal/js/journal-guide-filter.js','utf8'));
        const data=JSON.parse(fs.readFileSync(0,'utf8'));
        process.stdout.write(JSON.stringify({skills:data.roles.map(role=>JournalSkillSelection(data.skills,role)),
          catalog:data.filters.map(filters=>data.catalog.rows.filter(row=>JournalCatalogMatches(row,filters)).map(row=>row.id))}));"""
        run = subprocess.run(['node', '-e', script], input=json.dumps({'skills': skills, 'roles': roles,
                             'catalog': catalog, 'filters': filters}), text=True, encoding='utf-8', capture_output=True, check=True)
        actual = json.loads(run.stdout)
        self.assertEqual(actual['skills'], expected_skills)
        self.assertEqual(actual['catalog'], expected_catalog)
        self.assertEqual(expected_skills[2]['skills'][0]['parent'], expected_skills[1]['skills'][0]['id'])
        self.assertEqual(skills, original)

    def test_failed_publish_and_source_race_keep_previous_index(self):
        files.refresh_journal_snapshot()
        before = files.read_index()
        with patch.object(files, '_save', side_effect=OSError('模拟文件失败')), self.assertRaises(OSError):
            files.refresh_journal_snapshot()
        self.assertEqual(files.read_index(), before)
        source = files._source()
        with patch.object(files, '_source', side_effect=[source, (source[0], source[1], '已变化')]):
            self.assertFalse(files.refresh_journal_snapshot())
        self.assertEqual(files.read_index(), before)
        self.assertTrue(files.refresh_journal_snapshot())

    def test_index_pointer_switch_is_atomic_and_reuses_unchanged_content(self):
        files.refresh_journal_snapshot()
        old = files.read_index()
        self.assertTrue(files.refresh_journal_snapshot())
        self.assertEqual(files.read_index(), old)
        self.boss.payload['sections'][2]['descriptions']['2'] = '新技能正文'
        self.boss.save()
        real_write = files._write
        observed = []
        def inspect(path, body):
            if path.name == 'index.json':
                observed.append(files.read_index()['generation'])
            real_write(path, body)
        with patch.object(files, '_write', side_effect=inspect):
            self.assertTrue(files.refresh_journal_snapshot())
        self.assertEqual(observed, [old['generation']])
        self.assertNotEqual(files.read_index()['generation'], old['generation'])

    def test_corrupt_files_fail_closed_and_offline_rebuild_repairs(self):
        files.refresh_journal_snapshot()
        index = files.read_index()
        path = files.snapshot_root() / index['instances']['10']
        path.write_text('{}', encoding='utf-8')
        with self.assertNumQueries(0), self.assertRaises(files.JournalSnapshotUnavailable):
            detail_data(self.factory.get('/'), 10)
        files.refresh_journal_snapshot()
        self.assertEqual(detail_data(self.factory.get('/'), 10)['instance']['id'], 10)

    def test_commit_events_rollback_idle_and_bulk_compensation(self):
        files.refresh_journal_snapshot()
        with self.assertNumQueries(0):
            self.assertFalse(files.refresh_journal_snapshot(poll=True))
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.boss.payload['description'] = '已提交的描述'
            self.boss.save(update_fields=['payload'])
        self.assertEqual(len(callbacks), 1)
        with patch.object(files.time, 'time', return_value=files.time.time() + 16):
            self.assertTrue(files.refresh_journal_snapshot(poll=True))
        revision = files._load(files.snapshot_root() / 'revision.json')
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(ValueError), transaction.atomic():
                self.boss.save()
                raise ValueError('回滚')
        self.assertEqual(callbacks, [])
        self.assertEqual(files._load(files.snapshot_root() / 'revision.json'), revision)
        payload = deepcopy(self.boss.payload)
        payload['description'] = '批量更新描述'
        JournalEncounter.objects.filter(pk=self.boss.pk).update(payload=payload)
        with patch.object(files.time, 'time', return_value=files.time.time() + 3620):
            self.assertTrue(files.refresh_journal_snapshot(poll=True))
        self.assertEqual(detail_data(self.factory.get('/'), 10)['boss']['description'], '批量更新描述')
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.state.save(update_fields=['sync_token'])
        self.assertEqual(callbacks, [])
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.state.save(update_fields=['active_release_id'])
        self.assertEqual(len(callbacks), 1)

    def test_publish_lock_and_offline_command(self):
        with files._lock(files.snapshot_root() / 'publish.lock'), self.assertNumQueries(0):
            self.assertFalse(files.refresh_journal_snapshot())
        output = StringIO()
        call_command('refresh_journal_snapshot', stdout=output)
        self.assertIn('发布完成', output.getvalue())

    def test_published_menu_cannot_accept_another_release_skill(self):
        files.refresh_journal_snapshot()
        index = files.read_index()
        instance = deepcopy(files.read_instance(index, 10))
        boss = instance['bosses'][0]
        original = deepcopy(files.read_skills(index, instance, boss, 2))
        original['coordinate']['release_id'] += 1
        boss['skills']['2'] = files._save(original)
        with self.assertNumQueries(0), self.assertRaises(files.JournalSnapshotUnavailable):
            files.read_skills(index, instance, boss, 2)

    def test_in_place_loot_revision_cannot_hit_previous_projection(self):
        from botend.services import journal_loot_snapshot as loot_files
        warm_journal()
        old = detail_data(self.factory.get('/'), 10)
        self.boss.payload['loot'][0]['name'] = '修订后的掉落名称'
        self.boss.save()
        files.refresh_journal_snapshot()
        with self.assertNumQueries(0):
            updated = detail_data(self.factory.get('/'), 10)
        self.assertEqual(old['loot_snapshot']['state'], 'ready')
        self.assertEqual(updated['loot_snapshot']['state'], 'building')
        self.assertEqual(updated['loot'], [])
        loot_files.refresh_journal_loot_snapshots(force=True)
        self.assertEqual(detail_data(self.factory.get('/'), 10)['loot'][0]['name'], '修订后的掉落名称')

    def test_loot_revision_during_build_does_not_publish_mixed_content(self):
        from botend.services import journal_loot_snapshot as loot_files
        from botend.portal.adventure_journal import build_loot_projection
        warm_journal()
        before = detail_data(self.factory.get('/'), 10)['loot']
        def change(*args):
            result = build_loot_projection(*args)
            for drop in self.boss.payload['loot']:
                drop['name'] = '构建期间变更'
            self.boss.save()
            return result
        with patch('botend.portal.adventure_journal.build_loot_projection', side_effect=change):
            self.assertEqual(loot_files.refresh_journal_loot_snapshots(force=True), [])
        self.assertEqual(detail_data(self.factory.get('/'), 10)['loot'], before)
