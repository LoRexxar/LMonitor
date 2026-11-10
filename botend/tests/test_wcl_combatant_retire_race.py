"""Real independent connections, command transactions and SELECT interleavings."""
from datetime import timedelta
import io
import json
import threading

from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from botend.models import WclCombatantDocument, WclCombatantFact, WclCombatantSnapshot
from botend.services import wcl_combatant_store as store
from botend.tests.test_wcl_combatant_store import event


class CombatantRetirementRaceTests(TransactionTestCase):
    def read_during_retirement(self, *, before_payload):
        old = timezone.now() - timedelta(days=2)
        cutoff = old + timedelta(hours=1)
        # Full-fight teammates, shared documents, an already compact actor, and
        # a too-new actor must all retain their normal read/cutoff semantics.
        store.persist_events('Report', {11: [event(3)]}, fetched_at=old)
        for actor in (1, 2, 4):
            WclCombatantSnapshot.objects.create(
                report_code='Report', fight_id=11, actor_id=actor,
                payload_json=event(actor), fetched_at=old if actor != 4 else cutoff + timedelta(seconds=1))
        ready, committed = threading.Event(), threading.Event()
        receipts, errors, raw_connections = [], [], []
        connection.ensure_connection()
        reader_connection = connection.connection

        def retire():
            try:
                if not ready.wait(5):
                    raise AssertionError('reader never reached the selected SELECT')
                connections['default'].ensure_connection()
                raw_connections.append(connections['default'].connection)
                out = io.StringIO()
                call_command('migrate_wcl_combatant_facts', apply=True, retire_legacy=True, stdout=out)
                receipts.append(json.loads(out.getvalue()))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()
                committed.set()

        intercepted = []

        def pause(execute, sql, params, many, context):
            legacy_select = sql.lstrip().upper().startswith('SELECT') and 'wow_wcl_combatant_snapshot' in sql
            payload = 'payload_json' in sql
            if legacy_select and payload == before_payload and not intercepted:
                intercepted.append(sql)
                ready.set()
                if not committed.wait(5):
                    raise AssertionError('retirement did not commit')
                if errors:
                    raise errors[0]
            return execute(sql, params, many, context)

        worker = threading.Thread(target=retire)
        worker.start()
        try:
            with connection.execute_wrapper(pause):
                got = store.load_facts([{'report_code': 'Report', 'fight_id': 11}], fetched_before=cutoff)
        finally:
            ready.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(len(intercepted), 1)
        self.assertIsNot(raw_connections[0], reader_connection)
        self.assertEqual(tuple(receipts[0][key] for key in ('created', 'verified', 'retired')), (3, 3, 3))
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        self.assertEqual(WclCombatantFact.objects.count(), 4)
        self.assertEqual(WclCombatantDocument.objects.count(), 2)
        self.assertEqual(got[('Report', 11)], [store.canonical_event(event(actor)) for actor in (1, 2, 3)])
        self.assertFalse(store.load_facts([{'report_code': 'Report', 'fight_id': 11}],
                                         fetched_before=old - timedelta(microseconds=1)))

    def test_retirement_commits_before_legacy_identity_select(self):
        self.read_during_retirement(before_payload=False)

    def test_retirement_commits_before_legacy_payload_select(self):
        self.read_during_retirement(before_payload=True)
