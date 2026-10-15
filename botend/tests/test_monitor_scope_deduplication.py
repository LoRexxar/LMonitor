from datetime import timedelta
from io import StringIO
import json
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from botend.models import MonitorTask, MonitorTaskLease
from botend.plugin_sync import claim_next_monitor_task, claim_monitor_task, release_monitor_task_lease


class DuplicateMonitorScopeTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.now = timezone.now()
        values = dict(type=18, target='', env_limit=0, is_active=True,
                      last_scan_time=self.now-timedelta(days=2), wait_time=3600)
        self.canonical = MonitorTask.objects.create(name='PortalPostMonitor', **values)
        self.alias = MonitorTask.objects.create(name='portal_post', **{**values, 'wait_time': 1})

    def test_auto_claim_runs_only_canonical_not_fast_alias(self):
        task = claim_next_monitor_task(now=self.now, lease_owner='one')
        self.assertEqual(task.pk, self.canonical.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now, lease_owner='two'))
        release_monitor_task_lease(task.pk, 'one')
        self.assertIsNone(claim_next_monitor_task(now=self.now+timedelta(seconds=2), lease_owner='two'))

    def test_manual_alias_claim_excludes_auto_and_other_manual(self):
        task = claim_monitor_task(self.alias.pk, now=self.now, lease_owner='manual')
        self.assertEqual(task.pk, self.alias.pk)
        self.assertIsNone(claim_monitor_task(self.canonical.pk, now=self.now, lease_owner='other'))
        self.assertIsNone(claim_next_monitor_task(now=self.now, lease_owner='auto'))

    def test_inactive_alias_with_live_lease_still_protects_scope(self):
        claim_monitor_task(self.alias.pk, now=self.now, lease_owner='manual')
        MonitorTask.objects.filter(pk=self.alias.pk).update(is_active=False)
        self.assertIsNone(claim_next_monitor_task(now=self.now, lease_owner='auto'))

    def test_different_exact_target_remains_independent(self):
        self.alias.target = 'different-target'
        self.alias.save(update_fields=['target'])
        self.assertIsNotNone(claim_next_monitor_task(now=self.now, lease_owner='one'))
        self.assertIsNotNone(claim_next_monitor_task(now=self.now, lease_owner='two'))

    def test_command_is_dry_run_then_idempotent_non_destructive_disable(self):
        before = list(MonitorTask.objects.order_by('pk').values())
        output = StringIO()
        call_command('consolidate_monitor_scope', keep_id=self.canonical.pk,
                     disable_id=[self.alias.pk], stdout=output)
        self.assertEqual(json.loads(output.getvalue())['status'], 'would_disable')
        self.assertEqual(before, list(MonitorTask.objects.order_by('pk').values()))
        for _ in range(2):
            call_command('consolidate_monitor_scope', keep_id=self.canonical.pk,
                         disable_id=[self.alias.pk], apply=True, stdout=StringIO())
        after = list(MonitorTask.objects.order_by('pk').values())
        before[1]['is_active'] = False
        self.assertEqual(before, after)

    def test_command_rejects_mismatched_scope_and_running_scope(self):
        self.alias.target = 'different-target'
        self.alias.save(update_fields=['target'])
        with self.assertRaises(CommandError):
            call_command('consolidate_monitor_scope', keep_id=self.canonical.pk,
                         disable_id=[self.alias.pk], apply=True, stdout=StringIO())
        self.alias.target = ''
        self.alias.save(update_fields=['target'])
        claim_monitor_task(self.alias.pk, now=self.now, lease_owner='manual')
        with self.assertRaises(CommandError):
            call_command('consolidate_monitor_scope', keep_id=self.canonical.pk,
                         disable_id=[self.alias.pk], apply=True, stdout=StringIO())
        self.alias.refresh_from_db()
        self.assertTrue(self.alias.is_active)

    def test_disabled_canonical_does_not_disable_remaining_scope(self):
        self.canonical.is_active = False
        self.canonical.save(update_fields=['is_active'])
        self.assertEqual(claim_next_monitor_task(now=self.now, lease_owner='one').pk, self.alias.pk)
