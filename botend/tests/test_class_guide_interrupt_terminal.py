"""Interrupts must persist a terminal batch without swallowing the signal."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from botend.guide_models import ClassGuideSyncRun
from botend.models import MonitorTask, MonitorTaskLease
from botend.services.class_guide_monitor import get_guide_monitor_task
from botend.services.class_guide_service import sync_guides


class GuideInterruptTerminalTests(TestCase):
    def test_keyboard_interrupt_preserves_accounting_and_owner_fence(self):
        task = get_guide_monitor_task()
        task.notes = 'Isolated regression authorization'
        task.save(update_fields=['notes'])
        url = 'https://maxroll.gg/wow/class-guides/arcane-mage-raid-guide'
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'urls.json').write_text(json.dumps([url, url]))
            # Invalid local source deliberately records a per-article failure;
            # no network, translation engine or import logic is substituted.
            (root / 'arcane-mage-raid-guide.json').write_text('{}')
            for replace_owner in (False, True):
                with self.subTest(replace_owner=replace_owner):
                    interrupted = KeyboardInterrupt()
                    recorded = []
                    def log(message):
                        recorded.append(json.loads(message))
                        if replace_owner:
                            MonitorTaskLease.objects.filter(task=task).update(owner='replacement-owner')
                            MonitorTask.objects.filter(pk=task.pk).update(flag='replacement flag')
                        raise interrupted
                    try:
                        with self.assertRaises(KeyboardInterrupt) as raised:
                            sync_guides(cache_dir=root, translate=False, log=log)
                        self.assertIs(raised.exception, interrupted)
                        run = ClassGuideSyncRun.objects.latest('pk')
                        self.assertEqual(run.status, 'failed')
                        self.assertTrue(run.error.strip())
                        self.assertIn('KeyboardInterrupt', run.error)
                        self.assertIsNotNone(run.finished_at)
                        self.assertEqual(len(recorded), 1)
                        self.assertEqual(run.results, recorded)
                        self.assertEqual(run.results[0]['status'], 'failed')
                        task.refresh_from_db()
                        if replace_owner:
                            self.assertEqual(task.flag, 'replacement flag')
                            self.assertEqual(MonitorTaskLease.objects.get(task=task).owner, 'replacement-owner')
                        else:
                            self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())
                            self.assertIn('failed', task.flag)
                    finally:
                        # Only isolated fixture cleanup; never release a real lease.
                        MonitorTaskLease.objects.filter(task=task).delete()
