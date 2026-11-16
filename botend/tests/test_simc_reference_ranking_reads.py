"""The ranking HTTP read model never resolves simulation inputs."""
from contextlib import ExitStack
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from botend.models import SimcReferenceRankingEntry, SimcReferenceSimulationConfig
from botend.services.simc_reference_rankings import current_rankings, ranking_entry
from botend.tests import test_simc_reference_rankings as ranking_tests


class ReferenceRankingReadTests(TestCase):
    setUp = ranking_tests.ReferenceRankingTests.setUp
    result = ranking_tests.ReferenceRankingTests.result
    alternate_talent = ranking_tests.ReferenceRankingTests.alternate_talent

    def test_admin_queries_do_not_grow_with_combinations_or_parse_titles(self):
        """冷读和重复读均不得逐行查询天赋字段或解析攻略。"""
        from botend.services.simc_reference_rankings import admin_ranking_page_context
        anchor = self.result(100)
        with CaptureQueriesContext(connection) as single:
            admin_ranking_page_context(self.config.settings_id)
        # 仅构造读取端投影夹具，覆盖线上实测的 415 行规模，不执行模拟。
        from botend.models import SimcTalentString, SimcReferenceSimulation
        talents = SimcTalentString.objects.bulk_create([
            SimcTalentString(name=f'规模测试天赋 {number}', spec=self.talent.spec,
                            talent=self.talent.talent, hero_talent_names=['Slayer'], is_system=True, default_apl=self.apl)
            for number in range(414)
        ])
        self.publication.talents.add(*talents)
        entry = SimcReferenceRankingEntry.objects.get(result=anchor)
        results = SimcReferenceSimulation.objects.bulk_create([
            SimcReferenceSimulation(config=self.config, scenario_id=entry.scenario_id,
                profile=self.profile, talent=talent, apl=self.apl, requested_by_id=self.user.pk,
                status='completed', dps=200 + number, snapshot={}, signature=f'{number + 1:064x}')
            for number, talent in enumerate(talents)
        ])
        SimcReferenceRankingEntry.objects.bulk_create([
            SimcReferenceRankingEntry(config=self.config, scenario_id=entry.scenario_id,
                profile=self.profile, talent=talent, apl=self.apl, result=result,
                class_key=entry.class_key, spec_key=entry.spec_key,
                comparison_key=entry.comparison_key, input_signature=result.signature,
                talent_display_name=talent.name)
            for talent, result in zip(talents, results)
        ])
        SimcReferenceRankingEntry.objects.filter(pk=entry.pk).update(is_current=True)
        self.client.force_login(self.user)
        with patch('botend.services.simc_talent_titles.apply_talent_display_names',
                   side_effect=AssertionError('榜单读取不得解析天赋标题')):
            with CaptureQueriesContext(connection) as many:
                board = admin_ranking_page_context(self.config.settings_id)
            self.assertEqual(len(board['entries']), 1)
            self.assertEqual(board['entries'][0]['dps'], 613)
            self.assertEqual(len(many), len(single))
            self.assertLessEqual(len(many), 3)
            from time import perf_counter
            for attempt in range(2):
                start = perf_counter()
                with CaptureQueriesContext(connection) as http:
                    response = self.client.get(reverse('simc_reference_rankings_entry'))
                print(f'本地榜单规模核验：415 行，第 {attempt + 1} 次请求，{len(http)} 次查询，{perf_counter() - start:.3f} 秒')
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(len(http), 12)
                selected = ' '.join(q['sql'].split(' FROM ')[0] for q in http)
                for forbidden in ('"snapshot"', '"source_markdown"', '"content_markdown"', '"talent"'):
                    self.assertNotIn(forbidden, selected)

    def test_unfilled_title_uses_stored_name_without_lazy_rebuild(self):
        from botend.services.simc_reference_rankings import admin_ranking_page_context
        self.result(100)
        SimcReferenceRankingEntry.objects.update(talent_display_name='')
        with patch('botend.services.simc_talent_titles.apply_talent_display_names',
                   side_effect=AssertionError('不得在访问时回填')):
            board = admin_ranking_page_context(self.config.settings_id)
        self.assertEqual(board['entries'][0]['talent'], self.talent.name)
        self.assertEqual(SimcReferenceRankingEntry.objects.get().talent_display_name, '')

    def test_index_only_http_reads_and_payload_projection(self):
        anchor = self.result(100)
        for number in range(8):
            self.result(200 + number, talent=self.alternate_talent())
        self.result(500, 5)
        config = SimcReferenceSimulationConfig.objects.select_related('settings__template').get(pk=self.config.pk)
        self.client.force_login(self.user)
        with ExitStack() as stack:
            for target in (
                'botend.services.simc_reference_simulations.snapshot',
                'botend.services.simc_reference_rankings.snapshot',
                'botend.services.simc_reference_simulations.validate_config',
                'botend.services.simc_player_config.parse_manual_player_config',
                'botend.services.simc_player_config.load_item_tooltip_metadata',
            ):
                stack.enter_context(patch(target, create=True, side_effect=AssertionError('heavy ranking GET: ' + target)))
            with CaptureQueriesContext(connection) as queries:
                rankings = current_rankings(config)
            self.assertLessEqual(len(queries), 3)
            self.assertEqual(rankings[(anchor.config_id, anchor.signature)]['overall_total'], 1)
            self.assertEqual(rankings[(anchor.config_id, anchor.signature)]['spec_total'], 9)
            with CaptureQueriesContext(connection) as entry_queries:
                entry = ranking_entry(self.config.pk)
            self.assertLessEqual(len(entry_queries), 1)
            self.assertEqual(entry, (self.config.pk, anchor.signature, 'overall'))
            entry_url = reverse('portal_simc_reference_rankings_entry')
            with CaptureQueriesContext(connection) as http_queries:
                response = self.client.get(entry_url, {'config': self.config.pk}, follow=True)
            self.assertLessEqual(len(http_queries), 12)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.redirect_chain), 1)
            self.assertEqual(len(response.context['entries']), 1)
            self.assertNotContains(response, 'data-ranking-current')
            self.assertContains(response, '当前配置未上榜')
        selected = ' '.join(query['sql'].split(' FROM ')[0] for query in queries)
        for forbidden in ('"snapshot"', '"player_equipment"', '"content"', '"talent"', '"current_version"'):
            self.assertNotIn(forbidden, selected)

    def test_stored_legacy_signature_remains_the_public_ranking_identity(self):
        from botend.services import simc_reference_simulations as source
        from botend.services.simc_reference_ranking_index import sync_result
        anchor = self.result(100)
        anchor.snapshot['backend'] += ['old-binary-version', 'old-game-build']
        anchor.signature = source.tasks._compute_content_hash(anchor.snapshot)
        anchor.save(update_fields=['signature', 'snapshot'])
        sync_result(anchor.pk)
        stored_signature = anchor.signature
        self.assertIn((anchor.config_id, stored_signature), current_rankings(self.config))
        self.assertEqual(ranking_entry(self.config.pk), (self.config.pk, stored_signature, 'overall'))
        self.client.force_login(self.user)
        url = reverse('portal_simc_reference_rankings', args=[self.config.pk, stored_signature, 'overall'])
        self.assertContains(self.client.get(url), 'data-ranking-current', count=1)
        anchor.refresh_from_db()
        self.assertEqual(anchor.signature, stored_signature)
        self.assertEqual(anchor.snapshot['backend'][-2:], ['old-binary-version', 'old-game-build'])

    def test_no_index_is_empty_without_lazy_rebuild(self):
        self.result(100)
        SimcReferenceRankingEntry.objects.all().delete()
        self.client.force_login(self.user)
        with patch('botend.services.simc_reference_ranking_index.sync_result', side_effect=AssertionError('lazy write')):
            self.assertEqual(current_rankings(self.config), {})
            self.assertIsNone(ranking_entry())
            self.assertContains(self.client.get(reverse('portal_simc_reference_rankings_entry')), '暂无可展示的模拟结果')
        self.assertFalse(SimcReferenceRankingEntry.objects.exists())
