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
            self.assertEqual(rankings[(anchor.config_id, anchor.signature)]['overall_total'], 9)
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
            self.assertEqual(len(response.context['entries']), 9)
            self.assertContains(response, 'data-ranking-current', count=1)
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
