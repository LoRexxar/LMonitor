"""Real bulk resource retirement must invalidate the lightweight ranking pointers."""
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
from django.test import TestCase, override_settings
from botend.models import SimcProfile, SimcReferenceSimulation, SimcTask
from botend.tests import test_simc_reference_simulations as fixtures
from botend.tests import test_simc_default_player_templates as upstream_fixtures


class RankingBulkRetirementTests(TestCase):
    setUp = fixtures.ReferenceSimulationTests.setUp
    enqueue = fixtures.ReferenceSimulationTests.enqueue
    dispatch = fixtures.ReferenceSimulationTests.dispatch

    def publish_existing(self):
        from botend.models import SimcReferenceRankingEntry
        self.enqueue()
        fixtures.ReferenceSimulationTests.complete(self)
        self.assertEqual(SimcReferenceRankingEntry.objects.filter(is_current=True).count(), 2)
        return list(SimcReferenceSimulation.objects.order_by('pk').values_list('pk', 'signature', 'snapshot'))

    def assert_retired_without_new_tasks(self, frozen):
        from botend.models import SimcReferenceRankingEntry
        self.assertFalse(SimcReferenceRankingEntry.objects.filter(is_current=True).exists())
        self.assertEqual(SimcTask.objects.count(), 2)
        self.assertEqual(list(SimcReferenceSimulation.objects.order_by('pk').values_list('pk', 'signature', 'snapshot')), frozen)

    def test_upstream_import_retires_excluded_profile_and_talent(self):
        self.profile.source = SimcProfile.SOURCE_SIMC_UPSTREAM
        self.profile.system_key = 'simc_upstream:warrior_fury'
        self.profile.save(update_fields=['source', 'system_key'])
        self.talent.system_key = 'simc_upstream:warrior_fury'
        self.talent.save(update_fields=['system_key'])
        frozen = self.publish_existing()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Arms.simc').write_text(upstream_fixtures.DEFAULT_PLAYER.replace('spec=fury', 'spec=arms'))
            with patch('botend.management.commands.import_simc_player_templates.REQUIRED_PROFILE_SPECS', {('warrior', 'arms')}), patch('botend.management.commands.import_simc_player_templates.resolve_hero_talent_names', return_value=[]), self.captureOnCommitCallbacks(execute=True):
                call_command('import_simc_player_templates', source_dir=tmp, stdout=StringIO(), stderr=StringIO())
        self.profile.refresh_from_db()
        self.talent.refresh_from_db()
        self.assertFalse(self.profile.is_active)
        self.assertFalse(self.talent.is_active)
        self.assert_retired_without_new_tasks(frozen)

    def test_default_template_sync_retires_still_referenced_old_template(self):
        from botend.management.commands.update_simc_binary import Command
        self.template.source = self.template.SOURCE_SIMC_UPSTREAM
        self.template.spec = 'all'
        self.template.save()
        frozen = self.publish_existing()
        command = Command(stdout=StringIO(), stderr=StringIO())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'default.simc')
            path.write_text('optimal_raid=0\n')
            with override_settings(SIMC_CONFIG={'simc_template': str(path)}), self.captureOnCommitCallbacks(execute=True):
                command._sync_default_template()
        self.template.refresh_from_db()
        self.assertFalse(self.template.is_active)
        self.assertFalse(self.template.is_selectable)
        self.assert_retired_without_new_tasks(frozen)
