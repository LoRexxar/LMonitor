"""Publish the ranking reference through the normal successful simulation lifecycle."""
from unittest.mock import patch
from django.test import TestCase
from botend.tests import test_simc_reference_simulations as fixtures
from botend.models import SimcReferenceSimulation
from botend.services import simc_reference_simulations as service


class RankingPublicationTests(TestCase):
    setUp = fixtures.ReferenceSimulationTests.setUp
    enqueue = fixtures.ReferenceSimulationTests.enqueue
    dispatch = fixtures.ReferenceSimulationTests.dispatch

    def test_completed_runs_publish_current_index_without_extra_simulations(self):
        from botend.models import SimcReferenceRankingEntry
        from botend.services.simc_reference_rankings import current_rankings
        self.enqueue()
        fixtures.ReferenceSimulationTests.complete(self)
        refs = list(SimcReferenceSimulation.objects.filter(status='completed'))
        self.assertEqual(len(refs), 2)
        self.assertEqual(SimcReferenceRankingEntry.objects.filter(is_current=True).count(), 2)
        self.assertEqual(set(SimcReferenceRankingEntry.objects.values_list('result_id', flat=True)), {r.pk for r in refs})
        with patch('botend.services.simc_reference_simulations.snapshot', side_effect=AssertionError('HTTP read must not reparse a profile')):
            ranks = current_rankings(self.config)
        self.assertEqual(len(ranks), 2)
        self.assertEqual({r['overall_rank'] for r in ranks.values()}, {1})

    def test_binary_version_only_full_save_never_invalidates_or_rebuilds(self):
        from botend.models import SimcReferenceRankingEntry
        self.enqueue()
        fixtures.ReferenceSimulationTests.complete(self)
        with patch('botend.services.simc_reference_ranking_index.refresh_configs', side_effect=AssertionError('binary metadata must not rebuild rankings')), self.captureOnCommitCallbacks(execute=True):
            self.backend.current_version = 'b' * 40
            self.backend.game_build = '99.9.99999'
            self.backend.save()
        self.assertEqual(SimcReferenceRankingEntry.objects.filter(is_current=True).count(), 2)

    def test_index_failure_rolls_back_reference_completion_for_normal_retry(self):
        from botend.models import SimcReferenceRankingEntry
        self.enqueue()
        with patch('botend.services.simc_reference_ranking_index.sync_result', side_effect=RuntimeError('index write failed')):
            with self.assertRaisesRegex(RuntimeError, 'index write failed'):
                fixtures.ReferenceSimulationTests.complete(self)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='completed').count(), 0)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='submitted').count(), 2)
        self.assertEqual(SimcReferenceRankingEntry.objects.count(), 0)
        service.maintain_simulations()
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='completed').count(), 2)
        self.assertEqual(SimcReferenceRankingEntry.objects.filter(is_current=True).count(), 2)
