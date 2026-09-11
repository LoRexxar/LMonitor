"""首页动态必须以可见数据和北京时间为准。"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import resolve

from botend.journal_models import JournalRelease, JournalState
from botend.models import (
    MythicDungeon, MythicDungeonDataVersion, SeasonMeta, SpecDungeonRanking,
    WowHotfixReport, WowSkillDiffReport, WowTalentNodeMetadata, WowTalentVersion,
)
from botend.portal.updates import build_site_updates


class PortalSiteUpdatesTests(TestCase):
    now = datetime(2026, 9, 11, 2, tzinfo=dt_timezone.utc)

    def items(self):
        return {row['key']: row for row in build_site_updates(self.now)['items']}

    def report(self, at, build='1', content='职业改动'):
        row = WowSkillDiffReport.objects.create(to_build=build, content_md=content)
        WowSkillDiffReport.objects.filter(pk=row.pk).update(created_at=at)
        return row

    def test_shanghai_midnight_counts_reports_and_ignores_future_and_empty(self):
        self.report(self.now - timedelta(hours=11), 'old')
        self.report(self.now - timedelta(hours=9), 'today1')
        latest = self.report(self.now, 'today2')
        self.report(self.now, 'empty', '')
        self.report(self.now + timedelta(hours=1), 'future')
        row = self.items()['skill_diffs']
        self.assertEqual(row['today_count'], 2)
        self.assertEqual(row['status'], 'today')
        self.assertEqual(row['url'], f'/portal/wow-skill-diff/{latest.pk}/')

    def test_modifying_old_report_does_not_count_as_new_report(self):
        row = self.report(self.now - timedelta(days=2))
        WowSkillDiffReport.objects.filter(pk=row.pk).update(updated_at=self.now)
        item = self.items()['skill_diffs']
        self.assertEqual(item['status'], 'older')
        self.assertEqual(item['summary'], '今日暂无更新')
        self.assertEqual(item['today_count'], 0)

    def test_headline_explains_latest_changes(self):
        row = self.report(self.now)
        WowSkillDiffReport.objects.filter(pk=row.pk).update(class_count=3, spell_count=17)
        self.assertEqual(self.items()['skill_diffs']['headline'], '3 职业 · 17 项技能改动')
        release = JournalRelease.objects.create(build='test', status='completed', completed_at=self.now)
        JournalState.objects.create(active_release=release)
        self.assertEqual(self.items()['journal']['headline'], '首领技能与掉落已同步')

    def test_hotfix_is_independent_from_build_report(self):
        row = WowHotfixReport.objects.create(to_push=123, content_md='热修内容')
        WowHotfixReport.objects.filter(pk=row.pk).update(created_at=self.now)
        items = self.items()
        self.assertEqual(items['hotfixes']['today_count'], 1)
        self.assertEqual(items['skill_diffs']['status'], 'empty')

    def test_only_active_completed_journal_counts(self):
        release = JournalRelease.objects.create(build='old', status='completed', completed_at=self.now - timedelta(days=1))
        JournalState.objects.create(active_release=release)
        JournalRelease.objects.create(build='failed', status='failed', completed_at=self.now)
        JournalRelease.objects.create(build='staged', status='completed', completed_at=self.now)
        self.assertEqual(self.items()['journal']['status'], 'older')
        release.status = 'failed'
        release.save()
        self.assertEqual(self.items()['journal']['status'], 'empty')

    def test_rankings_only_use_current_season_and_dungeons(self):
        season = SeasonMeta.objects.create(season_key='test-current', season_name='当前赛季', mplus_zone_id=1, raid_zone_id=1, mplus_encounters=[{'id': 1}])
        for season_id, dungeon_id, at in ((season.pk, 1, self.now - timedelta(days=1)), (season.pk, 99, self.now), (99999, 1, self.now)):
            SpecDungeonRanking.objects.create(season_id=season_id, dungeon_id=dungeon_id, dps=100, last_updated=at)
        self.assertEqual(self.items()['mplus']['status'], 'older')
        season.gear_synced_at = self.now
        season.gear_batch_key = 'unpublished'
        season.save()
        self.assertEqual(self.items()['gear']['status'], 'empty')

    def test_mdt_requires_active_import_with_dungeons(self):
        version = MythicDungeonDataVersion.objects.create(key='test', label='测试', is_active=True, imported_at=self.now)
        self.assertEqual(self.items()['mdt']['status'], 'empty')
        MythicDungeon.objects.create(data_version=version, key='one', name='测试副本')
        self.assertEqual(self.items()['mdt']['status'], 'today')

    def test_talents_ignore_configuration_edits_and_localization_only(self):
        version = WowTalentVersion.objects.create(key='test-version', is_active=True, is_default_simulator=True)
        WowTalentNodeMetadata.all_objects.create(talent_version=version, node_id=1, last_updated=self.now - timedelta(days=1))
        WowTalentNodeMetadata.all_objects.create(talent_version=version, localization_only=True, name_kind='item', reference_id=999, last_updated=self.now)
        self.assertEqual(self.items()['talents']['status'], 'older')

    def test_today_first_and_links_resolve(self):
        release = JournalRelease.objects.create(build='test', status='completed', completed_at=self.now)
        JournalState.objects.create(active_release=release)
        payload = build_site_updates(self.now)
        self.assertEqual(payload['date'], '2026-09-11')
        self.assertEqual(payload['items'][0]['key'], 'journal')
        self.assertEqual(payload['today_modules'], 1)
        self.assertEqual(len(payload['items']), 8)
        for row in payload['items']:
            self.assertIsNotNone(resolve(row['url']))

    def test_public_endpoint_and_cache(self):
        cache.clear()
        with patch('botend.portal.updates.timezone.now', return_value=self.now):
            response = self.client.get('/portal/api/site-updates/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['timezone'], 'Asia/Shanghai')
        with patch('botend.portal.updates.build_site_updates', side_effect=AssertionError('不应重复查询')):
            self.assertEqual(self.client.get('/portal/api/site-updates/').status_code, 200)
        cache.clear()
