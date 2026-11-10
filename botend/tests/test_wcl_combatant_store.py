from copy import deepcopy
from datetime import timedelta
import importlib

from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import WclCombatantSnapshot
from botend.services import wcl_combatant_store as store


def event(actor=1, fight=11):
    return dict(sourceID=actor, specID=251, fight=fight, type='combatantinfo', timestamp=42,
                source=dict(id=actor, name=f'Player{actor}', server='Realm', type='Player', unused='x'),
                gear=[{'id': 123, 'gems': [{'id': 77}], 'unknown': [1, 2]}],
                talentTree=[{'id': 456, 'rank': 2, 'extra': {'x': True}}],
                race='Human', critMelee=-1, critSpell=8, critRanged=9,
                hasteMelee=0, hasteSpell=4, hasteRanged=5, mastery=6,
                versatilityDamageDone=-2, versatility=3, versa=4,
                unused=[{'large': 'transport-only'}])


class CompactStoreTests(TestCase):
    def models(self):
        from botend.models import WclCombatantDocument, WclCombatantFact
        return WclCombatantDocument, WclCombatantFact

    def test_projection_is_lossless_for_consumers_and_independent(self):
        from botend.services.wcl_combatant_snapshot import combatant_stats
        raw = event()
        projected = store.canonical_event(raw)
        self.assertEqual(combatant_stats(projected), combatant_stats(raw))
        self.assertEqual(projected['gear'], raw['gear'])
        self.assertEqual(projected['talentTree'], raw['talentTree'])
        self.assertEqual(projected['source'], {k: raw['source'][k] for k in ('id', 'name', 'server')})
        self.assertNotIn('timestamp', projected)
        self.assertNotIn('fight', projected)
        projected['gear'][0]['gems'].clear()
        self.assertEqual(raw['gear'][0]['gems'], [{'id': 77}])
        self.assertEqual(store.canonical_event({'sourceID': 1}), {'sourceID': 1})

    def test_bulk_dedup_all_actors_and_immutable_replay(self):
        Document, Fact = self.models()
        raw = [event(i) for i in range(1, 31)]
        old = timezone.now() - timedelta(days=2)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(store.persist_events('Report', {11: raw}, fetched_at=old), 30)
        self.assertLess(len(queries), 25)
        self.assertEqual(Document.objects.count(), 2)
        self.assertEqual(Fact.objects.count(), 30)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 0)
        with CaptureQueriesContext(connection) as replay_queries:
            self.assertEqual(store.persist_events('Report', {11: raw}), 0)
        self.assertFalse([q for q in replay_queries if q['sql'].lstrip().upper().startswith(
            ('INSERT ', 'UPDATE ', 'DELETE '))])
        self.assertEqual(set(Fact.objects.values_list('fetched_at', flat=True)), {old})
        facts = store.load_facts([dict(report_code='Report', fight_id=11)])
        self.assertEqual(facts[('Report', 11)], [store.canonical_event(x) for x in raw])
        self.assertEqual(store.fact_revision(), (None, Fact.objects.order_by('-pk').first().pk))
        row = Fact.objects.first()
        self.assertNotIn('gear', row.identity_json)
        self.assertNotIn('unused', row.attributes_json)
        with self.assertRaises(ProtectedError):
            row.gear_document.delete()
        changed = deepcopy(raw)
        changed[-1]['mastery'] = 987
        with self.assertRaises(ValueError):
            store.persist_events('Report', {11: changed, 12: [event(1, 12)]})
        self.assertFalse(Fact.objects.filter(fight_id=12).exists())
        self.assertEqual(Document.objects.count(), 2)

    def test_legacy_equivalence_cutoff_merge_and_conflict(self):
        Document, Fact = self.models()
        old = timezone.now() - timedelta(days=2)
        cutoff = old + timedelta(hours=1)
        raw = event()
        WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11, actor_id=1,
                                           payload_json=raw, fetched_at=old)
        records = [dict(report_code='Report', fight_id=11)]
        before = store.load_facts(records, fetched_before=cutoff)
        self.assertEqual(store.persist_events('Report', {11: [raw, event(2)]}), 2)
        self.assertEqual(Fact.objects.get(actor_id=1).fetched_at, old)
        self.assertEqual(store.load_facts(records, fetched_before=cutoff), before)
        self.assertEqual(len(store.load_facts(records)[('Report', 11)]), 2)
        self.assertFalse(store.load_facts(records, fetched_before=old - timedelta(seconds=1)))
        # New wins, even if a legacy row is subsequently corrupted.
        legacy = WclCombatantSnapshot.objects.get()
        legacy.payload_json = {**raw, 'mastery': 99}
        legacy.save(update_fields=['payload_json'])
        self.assertEqual(store.load_facts(records)[('Report', 11)][0]['mastery'], raw['mastery'])
        with self.assertRaises(ValueError):
            store.persist_events('Report', {11: [raw]})

    def test_empty_missing_and_namespaced_documents(self):
        Document, Fact = self.models()
        raw = event()
        raw['gear'] = []
        raw['talentTree'] = []
        store.persist_events('Report', {11: [raw]})
        self.assertEqual(Document.objects.count(), 2)
        self.assertEqual(set(Document.objects.values_list('kind', flat=True)), {'gear', 'talents'})
        row = event(2)
        del row['gear']
        del row['talentTree']
        store.persist_events('Report', {11: [row]})
        loaded = store.load_facts([dict(report_code='Report', fight_id=11)])[('Report', 11)]
        self.assertEqual(loaded[0]['gear'], [])
        self.assertNotIn('gear', loaded[1])
        self.assertIsNone(Fact.objects.get(actor_id=2).gear_document_id)

    def test_legacy_conflict_rolls_back_and_kind_hashes_are_stable(self):
        Document, Fact = self.models()
        raw = event()
        WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11, actor_id=1,
                                           payload_json={**raw, 'race': 'Orc'})
        with self.assertRaises(ValueError):
            store.persist_events('Report', {11: [event(2), raw]})
        self.assertFalse(Fact.objects.exists())
        self.assertFalse(Document.objects.exists())

    def test_multiple_batches_preserve_all_fights_actors_and_reuse_documents(self):
        Document, Fact = self.models()
        by_fight = {fight: [event(actor, fight) for actor in range(70, 0, -1)]
                    for fight in (11, 12, 13)}
        self.assertEqual(store.persist_events('Report', by_fight), 210)
        self.assertEqual(Document.objects.count(), 2)
        self.assertEqual(Fact.objects.count(), 210)
        records = [dict(report_code='Report', fight_id=fight) for fight in by_fight]
        got = store.load_facts(records + records)
        self.assertEqual(set(got), {('Report', fight) for fight in by_fight})
        for fight in by_fight:
            self.assertEqual(got[('Report', fight)],
                             [store.canonical_event(event(actor, fight)) for actor in range(1, 71)])

    def test_compact_reads_skip_covered_legacy_payloads_and_raw_only_replay(self):
        Document, Fact = self.models()
        raw = event()
        WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11, actor_id=1,
                                           payload_json=raw)
        store.persist_events('Report', {11: [raw]})
        original_hash = Fact.objects.get().source_hash
        self.assertEqual(store.persist_events('Report', {11: [{**raw, 'timestamp': 999}]}), 0)
        self.assertEqual(Fact.objects.get().source_hash, original_hash)
        with CaptureQueriesContext(connection) as queries:
            loaded = store.load_facts([dict(report_code='Report', fight_id=11)])
        self.assertEqual(loaded[('Report', 11)], [store.canonical_event(raw)])
        self.assertFalse([q for q in queries if 'wow_wcl_combatant_snapshot' in q['sql']
                          and 'payload_json' in q['sql']])
        with self.assertNumQueries(0):
            self.assertFalse(store.load_facts([]))

    def test_compact_catchup_does_not_reload_shared_documents(self):
        Document, Fact = self.models()
        by_fight = {fight: [event(actor, fight) for actor in range(1, 61)]
                    for fight in (11, 12, 13)}
        store.persist_events('Report', by_fight)
        with CaptureQueriesContext(connection) as queries:
            got = store.load_facts([dict(report_code='Report', fight_id=fight) for fight in by_fight])
        self.assertEqual(Document.objects.count(), 2)
        self.assertEqual(sum(map(len, got.values())), 180)
        # One compact read + two shared-document prefetches + light legacy keys
        # + one empty compact catch-up; no second document transfer.
        self.assertEqual(len(queries), 5)
        documents = [q for q in queries if 'wow_wcl_combatant_document' in q['sql']]
        self.assertEqual(len(documents), 2)
        self.assertFalse([q for q in queries if 'wow_wcl_combatant_snapshot' in q['sql']
                          and 'payload_json' in q['sql']])

    def test_migration_reverse_guard(self):
        from django.apps import apps
        migration = importlib.import_module('botend.migrations.0274_wcl_compact_facts')
        migration.guard_reverse(apps, None)
        store.persist_events('Report', {11: [event()]})
        with self.assertRaises(RuntimeError):
            migration.guard_reverse(apps, None)
        Document, Fact = self.models()
        Fact.objects.all().delete()
        with self.assertRaises(RuntimeError):
            migration.guard_reverse(apps, None)
        Document.objects.all().delete()
        migration.guard_reverse(apps, None)
