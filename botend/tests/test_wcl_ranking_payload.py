from copy import deepcopy
import json

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor as Sync
from botend.models import SpecRaidRanking
from botend.services.wcl_ranking_payload import compact_ranking_talents
from botend.tests import test_spec_detail_ranking_monitor as fixture_module
from botend.wow.talents.parser import normalize_talent_payload


class RankingTalentPayloadTests(SimpleTestCase):
    def test_full_and_compact_have_identical_existing_reader_output(self):
        full = SpecDetailBase.parse_wcl_talents([
            {'talentID': 202, 'points': 0, 'row': 0, 'column': 0},
            {'talentID': 203, 'spellID': 999, 'points': 1, 'name': 'Source name', 'icon': 'source_icon'},
            'invalid entry'])
        full += SpecDetailBase.parse_wcl_talent_tree([{'id': 302, 'nodeID': 42, 'rank': 2}])
        before = deepcopy(full)
        compact = compact_ranking_talents(full)
        self.assertEqual(full, before)
        self.assertEqual(normalize_talent_payload(full), normalize_talent_payload(compact))
        self.assertEqual(len(full), len(compact))
        self.assertEqual(compact[0]['entry_id'], 202)
        self.assertEqual(compact[0]['talentID'], 202)  # management SQL and legacy readers
        self.assertEqual(compact[0]['node_id'], 202)  # dependency scanner
        self.assertEqual(compact[0]['points'], 0)
        self.assertEqual((compact[0]['row'], compact[0]['column']), (0, 0))
        self.assertNotIn('spell_id', compact[0])
        self.assertEqual(compact[1]['spell_id'], 999)
        self.assertEqual(compact[1]['name'], 'Source name')
        self.assertEqual(compact[1]['icon'], 'source_icon')
        self.assertTrue(compact[2]['invalid'])
        self.assertEqual(compact[3]['db2_trait_node_id'], 42)
        self.assertEqual(compact[3]['points'], 2)
        self.assertNotIn('rank', compact[3])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)))
        self.assertEqual(compact_ranking_talents(compact), compact)

    def test_conflicts_unknown_sources_order_and_unknown_fields_are_not_erased(self):
        rows = [
            {'source': 'wcl_log', 'talentID': 202, 'talent_id': 203, 'spellID': 123,
             'spell_id': 456, 'rank': 2, 'points': 1, 'choice_selection': 0, 'extra': {'raw': None}},
            {'source': 'profile', 'talentID': 202, 'talent_id': 202, 'icon': '', 'row': None},
            {'talentID': 202, 'talent_id': 202, 'icon': ''},
            {'source': 'wcl_log', 'talentID': 202, 'talent_id': '202'},
            {'source': 'wcl_log', 'talentID': 202, 'talent_id': 202},
            {'source': 'wcl_log', 'talentID': 202, 'talent_id': 202, 'node_id': 999},
            'unparsed', None,
        ]
        self.assertEqual(compact_ranking_talents(rows), rows)
        duplicate = [rows[0], rows[0]]
        self.assertEqual(compact_ranking_talents(duplicate), duplicate)


class RankingCompactStorageTests(TestCase):
    def record(self, **changes):
        obj = fixture_module.SpecDetailRankingMonitorDifferentialUpdateTests()._record(
            report_code='compact-report', fight_id=1, dps=100, last_updated=timezone.now())
        obj.talent_build_code = ''
        obj.talents_json = SpecDetailBase.parse_wcl_talents([{'talentID': 202, 'points': 1}])
        for name, value in changes.items():
            setattr(obj, name, deepcopy(value))
        return obj

    def sync(self, rows):
        with CaptureQueriesContext(connection) as queries:
            result = Sync._sync_ranking_records(
                SpecRaidRanking, {'season_id': 1, 'boss_id': 100, 'difficulty': 5}, rows,
                Sync.RAID_KEY_FIELDS, Sync.RAID_UPDATE_FIELDS)
        writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        return result, writes

    def test_unchanged_old_format_is_not_rewritten_and_scalar_change_keeps_json(self):
        old = self.record()
        old.save()
        full = deepcopy(old.talents_json)
        result, writes = self.sync([self.record(talents_json=compact_ranking_talents(full))])
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(writes, [])
        old.refresh_from_db()
        self.assertEqual(old.talents_json, full)
        result, writes = self.sync([self.record(dps=101)])
        self.assertEqual(result['updated'], 1)
        self.assertTrue(writes)
        self.assertFalse(any('talents_json' in q or 'gear_json' in q for q in writes))
        old.refresh_from_db()
        self.assertEqual(old.talents_json, full)

    def test_new_and_changed_facts_store_compact_then_repeated_full_input_is_noop(self):
        incoming = self.record()
        full = deepcopy(incoming.talents_json)
        result, _ = self.sync([incoming])
        self.assertEqual(result['created'], 1)
        saved = SpecRaidRanking.objects.get()
        self.assertEqual(saved.talents_json, compact_ranking_talents(full))
        self.assertEqual(incoming.talents_json, full)
        result, writes = self.sync([self.record()])
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(writes, [])
        changed = deepcopy(full)
        changed[0]['points'] = 2
        result, _ = self.sync([self.record(talents_json=changed)])
        self.assertEqual(result['updated'], 1)
        saved.refresh_from_db()
        self.assertEqual(saved.talents_json, compact_ranking_talents(changed))
        result, writes = self.sync([self.record(talents_json=changed)])
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(writes, [])
