"""Top-ten selection through real event caches and immutable snapshot files."""
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.services import raid_burst_aggregation as agg
from botend.services.raid_burst_events import EventFetchDeferred, cache_path
from botend.services.raid_burst_snapshot import digest, read_blob, write_json
from botend.tests.test_raid_burst_aggregation import EventClient


class CandidateClient(EventClient):
    def __init__(self, rows, modes=None):
        super().__init__()
        self.rows = {r['report_code']: r for r in rows}
        self.modes = modes or {}

    def _wcl_graphql(self, query, variables):
        mode = self.modes.get(variables['code'])
        if mode in ('permission', 'budget_wait'):
            self.calls.append(dict(variables))
            self._wcl_last_error = mode
            return None
        self.pi, self.no_lust = mode == 'pi', mode == 'no_lust'
        result = super()._wcl_graphql(query, variables)
        assert result is not None
        if mode == 'initial_pi' and variables.get('kind') == 'CombatantInfo':
            result['reportData']['report']['page']['data'][0]['auras'] = [{'ability': 10060}]
        if variables.get('kind') is None:
            row = self.rows[variables['code']]
            actor = result['reportData']['report']['masterData']['actors'][0]
            actor.update(name=row['character_name'], server=row['realm'])
        return result


class RaidBurstTopTenTests(SimpleTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='raid-burst-top10-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / '1/Warrior/Arms'
        self.scope = dict(season_id=1, class_name='Warrior', spec_name='Arms', boss_id=3470,
                          difficulty=5, key='1/Warrior/Arms/5/3470', label='test')
        self.config = agg.SUPPORTED['Warrior', 'Arms']

    def row(self, rank, name=None, **kwargs):
        return dict(dict(report_code=f'LOG{rank}', fight_id=1, character_name=name or f'Player{rank}',
                         realm='Test-Realm', region='US', class_name='Warrior', spec_name='Arms',
                         boss_id=3470, difficulty=5, dps=10000-rank), **kwargs)

    def build(self, rows, client):
        descriptor = agg.build_scope(rows, self.scope, self.config, self.root, nullcontext, client)
        payload = read_blob(self.directory, descriptor['blob'])
        assert payload is not None
        return descriptor, payload

    def test_backfill_unique_players_only_after_success_and_keep_source_ranks(self):
        rows = [self.row(1, 'Retry'), self.row(2, 'PiRetry'), self.row(3, 'retry', region='us'),
                self.row(4, 'RETRY', realm='test realm', region=' us '),
                self.row(5, 'piretry'), self.row(6), self.row(7, character_name=None)]
        rows += [self.row(n) for n in range(8, 20)]
        client = CandidateClient(rows, {'LOG1': 'permission', 'LOG2': 'pi', 'LOG6': 'no_lust'})
        _, payload = self.build(rows, client)
        self.assertEqual([s['rank'] for s in payload['samples']], [3, 5, 8, 9, 10, 11, 12, 13, 14, 15])
        self.assertEqual(payload['candidate_count'], 19)
        self.assertEqual(payload['attempted'], 14)
        self.assertEqual(payload['successful'], 12)
        self.assertEqual(payload['excluded_duplicate'], 1)
        self.assertEqual(payload['excluded_invalid'], 2)
        self.assertEqual(payload['excluded_pi'], 1)
        self.assertEqual(payload['excluded_no_lust'], 1)
        self.assertFalse(any(c['code'] in {'LOG4', 'LOG7', 'LOG16'} for c in client.calls))

    def test_underfilled_scope_and_initial_pi_are_not_filled_with_duplicates(self):
        rows = [self.row(1), self.row(2, 'Player1'), self.row(3)]
        client = CandidateClient(rows, {'LOG3': 'initial_pi'})
        _, payload = self.build(rows, client)
        self.assertEqual(len(payload['samples']), 1)
        self.assertEqual(payload['attempted'], 2)
        self.assertEqual(payload['candidate_count'], 3)
        self.assertEqual(payload['excluded_duplicate'], 1)
        self.assertEqual(payload['excluded_pi'], 1)

    def test_old_prepared_rebuilt_with_zero_http_and_old_published_untouched(self):
        rows = [self.row(n) for n in range(1, 13)]
        client = CandidateClient(rows)
        with patch.object(agg, 'SAMPLE_LIMIT', 100), patch.object(agg, 'SELECTION_POLICY', 'legacy-all'):
            old, before = self.build(rows, client)
        self.assertEqual(len(before['samples']), 12)
        manifest = {'schema': 1, 'scopes': [old]}
        index = self.directory / 'index.json'
        write_json(index, {'data': manifest, 'digest': digest(manifest)})
        published_bytes = index.read_bytes()
        raw_bytes = {cache_path(self.root, r): cache_path(self.root, r).read_bytes() for r in rows}
        client.calls.clear()
        new, payload = self.build(list(reversed(rows)), client)
        self.assertNotEqual(new['blob'], old['blob'])
        self.assertEqual(len(payload['samples']), 10)
        self.assertEqual(payload['attempted'], 10)
        self.assertEqual(payload['candidate_count'], 12)
        self.assertEqual(payload['selection_limit'], 10)
        self.assertEqual(client.calls, [])
        self.assertEqual(index.read_bytes(), published_bytes)
        self.assertTrue(all(path.read_bytes() == content for path, content in raw_bytes.items()))
        self.assertEqual(self.build(rows, client)[0], new)
        self.assertEqual(client.calls, [])

    def test_budget_defer_never_becomes_an_exclusion(self):
        rows = [self.row(1), self.row(2)]
        client = CandidateClient(rows, {'LOG2': 'budget_wait'})
        with self.assertRaises(EventFetchDeferred):
            self.build(rows, client)
        self.assertFalse((self.directory / 'prepared').exists())
        client.modes.clear()
        client.calls.clear()
        _, payload = self.build(rows, client)
        self.assertEqual(len(payload['samples']), 2)
        self.assertTrue(all(c['code'] == 'LOG2' for c in client.calls))
