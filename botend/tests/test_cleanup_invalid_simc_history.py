from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from botend.models import SimcProfile, SimcTask, SimcTaskArtifact, SimcTaskBatch


VALID_PLAYER = '''warrior="Valid"
level=90
spec=fury
talents=ABC
head=,id=1
main_hand=,id=2
'''


class CleanupInvalidSimcHistoryCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cleanup-owner', password='pwd')

    def _profile(self, name, *, mode='attribute_only', equipment=''):
        return SimcProfile.objects.create(
            user_id=self.user.id,
            name=name,
            spec='fury',
            player_config_mode=mode,
            player_equipment=equipment,
        )

    def _task(self, profile, name, **kwargs):
        return SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=profile.id,
            name=name,
            **kwargs,
        )

    def test_default_is_dry_run_and_reports_reasoned_counts(self):
        invalid = self._profile('missing baseline')
        self._task(invalid, 'not executable', current_status=3)
        out = StringIO()

        call_command('cleanup_invalid_simc_history', stdout=out)

        self.assertTrue(SimcProfile.objects.filter(id=invalid.id).exists())
        self.assertTrue(SimcTask.objects.filter(name='not executable').exists())
        report = out.getvalue()
        self.assertIn('DRY-RUN', report)
        self.assertIn('invalid_profiles=1', report)
        self.assertIn('deletable_tasks=1', report)

    def test_apply_deletes_invalid_profile_unrunnable_task_and_empty_batch(self):
        invalid = self._profile('bad baseline', equipment='not simc')
        batch = SimcTaskBatch.objects.create(user_id=self.user.id, name='invalid batch')
        task = self._task(invalid, 'bad task', batch=batch, current_status=3)

        call_command('cleanup_invalid_simc_history', apply=True, stdout=StringIO())

        self.assertFalse(SimcProfile.objects.filter(id=invalid.id).exists())
        self.assertFalse(SimcTask.objects.filter(id=task.id).exists())
        self.assertFalse(SimcTaskBatch.objects.filter(id=batch.id).exists())

    def test_preserves_valid_profiles_and_battlenet_identity(self):
        valid_manual = self._profile('valid manual', mode='manual_equipment', equipment=VALID_PLAYER)
        valid_bn = SimcProfile.objects.create(
            user_id=self.user.id,
            name='valid battlenet',
            spec='fury',
            player_config_mode='battlenet',
            battlenet_region='eu',
            battlenet_realm='kazzak',
            battlenet_character='player',
        )

        call_command('cleanup_invalid_simc_history', apply=True, stdout=StringIO())

        self.assertTrue(SimcProfile.objects.filter(id=valid_manual.id).exists())
        self.assertTrue(SimcProfile.objects.filter(id=valid_bn.id).exists())

    def test_preserves_historical_tasks_with_result_or_frozen_content(self):
        invalid = self._profile('legacy missing baseline')
        with_summary = self._task(invalid, 'has summary', current_status=2, result_summary='{"dps": 12345}')
        with_file = self._task(invalid, 'has report', current_status=2, result_file='legacy.html')
        with_frozen = self._task(invalid, 'has frozen input', current_status=2, final_simc_content='warrior="X"\nlevel=90')
        with_artifact = self._task(invalid, 'has artifact', current_status=2)
        SimcTaskArtifact.objects.create(
            task=with_artifact,
            artifact_type='html_report',
            file_path='simc_results/report.html',
        )

        call_command('cleanup_invalid_simc_history', apply=True, stdout=StringIO())

        self.assertFalse(SimcProfile.objects.filter(id=invalid.id).exists())
        for task in (with_summary, with_file, with_frozen, with_artifact):
            self.assertTrue(SimcTask.objects.filter(id=task.id).exists())

    def test_preserves_pending_and_running_tasks_and_their_profiles(self):
        pending_profile = self._profile('pending profile')
        running_profile = self._profile('running profile')
        pending = self._task(pending_profile, 'pending task', current_status=0)
        running = self._task(running_profile, 'running task', current_status=1)

        out = StringIO()
        call_command('cleanup_invalid_simc_history', apply=True, stdout=out)

        for profile in (pending_profile, running_profile):
            self.assertTrue(SimcProfile.objects.filter(id=profile.id).exists())
        for task in (pending, running):
            self.assertTrue(SimcTask.objects.filter(id=task.id).exists())
        self.assertIn('deletable_tasks=0', out.getvalue())

    def test_apply_rechecks_locked_rows_inside_transaction(self):
        source = open(
            'botend/management/commands/cleanup_invalid_simc_history.py',
            encoding='utf-8',
        ).read()
        atomic_start = source.index('with transaction.atomic():')
        locked_section = source[atomic_start:]
        self.assertIn('select_for_update()', locked_section)
        self.assertIn('_has_trustworthy_task_state', locked_section)
        self.assertIn('current_status', locked_section)

    def test_owner_filter_limits_cleanup_scope(self):
        first = self._profile('first invalid')
        other = User.objects.create_user(username='cleanup-other', password='pwd')
        second = SimcProfile.objects.create(
            user_id=other.id,
            name='second invalid',
            spec='fury',
            player_config_mode='attribute_only',
            player_equipment='',
        )

        call_command(
            'cleanup_invalid_simc_history',
            apply=True,
            user_id=self.user.id,
            stdout=StringIO(),
        )

        self.assertFalse(SimcProfile.objects.filter(id=first.id).exists())
        self.assertTrue(SimcProfile.objects.filter(id=second.id).exists())
