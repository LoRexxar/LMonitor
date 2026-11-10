"""Real SQLite ORM coverage: no mocked store/model/network implementation."""
import io
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import WclCombatantDocument, WclCombatantFact, WclCombatantSnapshot
from botend.services import wcl_combatant_store as store


class WclCombatantMigrationTests(TestCase):
    def setUp(self):
        self.when = timezone.now().replace(microsecond=123456) - timedelta(days=2)

    def legacy(self, actor=1, *, when=None, payload=None, code='MigrationFixture'):
        if payload is None:
            payload = dict(type='combatantinfo', sourceID=actor, fight=11, specID=251,
                           source={'id': actor, 'name': 'FixturePlayer', 'server': 'FixtureRealm'},
                           critMelee=100, gear=[{'id': 123, 'slot': 0}],
                           talentTree=[{'id': 456, 'rank': 1}])
        return WclCombatantSnapshot.objects.create(
            report_code=code, fight_id=11, actor_id=actor, payload_json=payload,
            fetched_at=when or self.when)

    def run_command(self, **kwargs):
        out = io.StringIO()
        call_command('migrate_wcl_combatant_facts', stdout=out, **kwargs)
        result = json.loads(out.getvalue())
        self.assertNotIn('MigrationFixture', out.getvalue())
        self.assertNotIn('FixturePlayer', out.getvalue())
        return result

    def test_default_dry_run_is_zero_write_and_payload_reads_clear_ordering(self):
        row = self.legacy()
        with CaptureQueriesContext(connection) as queries:
            receipt = self.run_command()
        self.assertEqual(receipt['processed'], 1)
        self.assertEqual(receipt['created'], 0)
        self.assertEqual(WclCombatantFact.objects.count(), 0)
        self.assertEqual(WclCombatantDocument.objects.count(), 0)
        self.assertTrue(WclCombatantSnapshot.objects.filter(pk=row.pk).exists())
        for query in queries:
            sql = query['sql'].upper()
            self.assertFalse(sql.startswith(('INSERT ', 'UPDATE ', 'DELETE ')), sql)
            if 'AS "RAW_TEXT"' in sql:
                self.assertNotIn('ORDER BY', sql)

    def test_real_copy_idempotency_timestamps_and_cutoff(self):
        first = self.legacy()
        second = self.legacy(2, when=self.when + timedelta(microseconds=7))
        receipt = self.run_command(apply=True)
        self.assertEqual((receipt['created'], receipt['verified']), (2, 2))
        self.assertEqual(WclCombatantSnapshot.objects.count(), 2)
        doc_count = WclCombatantDocument.objects.count()
        self.assertGreater(doc_count, 0)
        self.assertEqual(self.run_command(apply=True)['created'], 0)
        self.assertEqual(WclCombatantDocument.objects.count(), doc_count)
        for row in (first, second):
            fact = WclCombatantFact.objects.get(actor_id=row.actor_id)
            self.assertEqual(fact.fetched_at, row.fetched_at)
            ref = [{'report_code': row.report_code, 'fight_id': row.fight_id}]
            key = (row.report_code, row.fight_id)
            visible = store.load_facts(ref, fetched_before=row.fetched_at).get(key, [])
            self.assertIn(store.canonical_event(row.payload_json), visible)
            before = store.load_facts(ref, fetched_before=row.fetched_at - timedelta(microseconds=1)).get(key, [])
            self.assertNotIn(row.actor_id, [item['sourceID'] for item in before])

    def test_conflict_rolls_back_whole_invocation(self):
        self.legacy()
        conflict = self.legacy(2)
        store.persist_events(conflict.report_code, {11: [conflict.payload_json]}, fetched_at=conflict.fetched_at)
        WclCombatantFact.objects.filter(actor_id=conflict.actor_id).update(attributes_json={'critMelee': 999})
        doc_count = WclCombatantDocument.objects.count()
        with self.assertRaises(CommandError):
            self.run_command(apply=True, retire_legacy=True)
        self.assertEqual(WclCombatantFact.objects.count(), 1)
        self.assertEqual(WclCombatantDocument.objects.count(), doc_count)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 2)

    def test_timestamp_conflict_is_not_overwritten(self):
        row = self.legacy()
        store.persist_events(row.report_code, {11: [row.payload_json]}, fetched_at=row.fetched_at)
        WclCombatantFact.objects.filter(actor_id=row.actor_id).update(fetched_at=row.fetched_at + timedelta(microseconds=1))
        with self.assertRaises(CommandError):
            self.run_command(apply=True)
        self.assertEqual(WclCombatantFact.objects.get().fetched_at, row.fetched_at + timedelta(microseconds=1))
        self.assertTrue(WclCombatantSnapshot.objects.filter(pk=row.pk).exists())

    def test_retirement_requires_explicit_apply_and_only_deletes_selected_verified_pk(self):
        first, second, third = self.legacy(), self.legacy(2), self.legacy(3)
        with self.assertRaises(CommandError):
            self.run_command(retire_legacy=True)
        receipt = self.run_command(apply=True, retire_legacy=True, after_id=first.pk, limit=1)
        self.assertEqual((receipt['end_pk'], receipt['retired'], receipt['verified']), (second.pk, 1, 1))
        self.assertTrue(receipt['remaining'])
        self.assertEqual(set(WclCombatantSnapshot.objects.values_list('pk', flat=True)), {first.pk, third.pk})
        self.assertTrue(WclCombatantFact.objects.filter(actor_id=second.actor_id).exists())
        resumed = self.run_command(apply=True, retire_legacy=True, after_id=receipt['end_pk'])
        self.assertEqual(resumed['end_pk'], third.pk)
        self.assertFalse(resumed['remaining'])
        self.assertTrue(WclCombatantSnapshot.objects.filter(pk=first.pk).exists())

    def test_bad_payload_or_budget_preserves_legacy_and_receipt_checkpoint(self):
        good = self.legacy()
        bad = self.legacy(2, payload={'sourceID': 999})
        for kwargs in ({}, {'max_payload_bytes': 1}):
            out = io.StringIO()
            with self.assertRaises(CommandError):
                call_command('migrate_wcl_combatant_facts', apply=True, retire_legacy=True, stdout=out, **kwargs)
            receipt = json.loads(out.getvalue())
            self.assertEqual((receipt['end_pk'], receipt['created'], receipt['retired']), (0, 0, 0))
            self.assertEqual(WclCombatantFact.objects.count(), 0)
            self.assertEqual(set(WclCombatantSnapshot.objects.values_list('pk', flat=True)), {good.pk, bad.pk})

    def test_resume_boundaries_and_limits(self):
        row = self.legacy()
        self.assertEqual(self.run_command(after_id=row.pk)['processed'], 0)
        self.assertEqual(self.run_command(after_id=9223372036854775807)['end_pk'], 9223372036854775807)
        for kwargs in ({'after_id': -1}, {'after_id': 9223372036854775808},
                       {'limit': 0}, {'limit': 2001}, {'max_payload_bytes': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CommandError):
                self.run_command(**kwargs)

    def test_byte_budget_makes_bounded_prefix_progress_without_raising_limit(self):
        first, second = self.legacy(), self.legacy(2)
        from django.db import models
        from django.db.models.functions import Length, Cast
        sizes = dict(WclCombatantSnapshot.objects.annotate(
            n=Length(Cast('payload_json', models.TextField()))).values_list('pk', 'n'))
        budget = sizes[first.pk] * 24 + 4096
        receipt = self.run_command(apply=True, limit=500, max_payload_bytes=budget)
        self.assertEqual((receipt['processed'], receipt['end_pk']), (1, first.pk))
        self.assertTrue(receipt['remaining'])
        self.assertEqual(WclCombatantFact.objects.count(), 1)
        resumed = self.run_command(apply=True, after_id=receipt['end_pk'])
        self.assertEqual(resumed['end_pk'], second.pk)
        self.assertFalse(resumed['remaining'])

    def test_late_real_projection_conflict_rolls_back_prior_insert_and_delete(self):
        first, second = self.legacy(), self.legacy(2)
        from botend.management.commands import migrate_wcl_combatant_facts as command
        original = command._verify
        checks = 0
        def corrupt_then_verify(store_api, Fact, row, expected):
            nonlocal checks
            checks += 1
            if checks == 2:
                Fact.objects.filter(actor_id=row['actor_id']).update(attributes_json={'critMelee': 999})
            return original(store_api, Fact, row, expected)
        with patch.object(command, '_verify', side_effect=corrupt_then_verify), self.assertRaises(CommandError):
            self.run_command(apply=True, retire_legacy=True)
        self.assertEqual(WclCombatantFact.objects.count(), 0)
        self.assertEqual(WclCombatantDocument.objects.count(), 0)
        self.assertEqual(set(WclCombatantSnapshot.objects.values_list('pk', flat=True)), {first.pk, second.pk})

    def test_raw_drift_after_preflight_rolls_back_copy_and_retirement(self):
        row = self.legacy()
        from botend.management.commands import migrate_wcl_combatant_facts as command
        original = command._read_row
        reads = 0
        def drifting(queryset, pk):
            nonlocal reads
            reads += 1
            if reads == 2:
                WclCombatantSnapshot.objects.filter(pk=pk).update(payload_json=dict(row.payload_json, critMelee=999))
            return original(queryset, pk)
        # Instrument only the timing of a real database change, never the store.
        with patch.object(command, '_read_row', side_effect=drifting), self.assertRaises(CommandError):
            self.run_command(apply=True, retire_legacy=True)
        self.assertEqual(WclCombatantFact.objects.count(), 0)
        row.refresh_from_db()
        self.assertEqual(row.payload_json['critMelee'], 100)
