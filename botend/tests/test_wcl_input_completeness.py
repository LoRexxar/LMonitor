"""Real Base pagination, transport and ORM; only the HTTP boundary is controlled."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import WclApiBudget, WclCombatantFetchState, WclCombatantSnapshot
from botend.services.wcl_api_budget import account_key
from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants


def sample(actor=232):
    return dict(report_code='Report', fight_id=11, character_name=f'Player{actor}',
                realm='Burning-Legion', region='eu', class_name='DeathKnight', spec_name='Frost')


def event(actor=232):
    # Shape checked against the read-only 3,850-event audit: server is a string,
    # masterData player identity is source.id (there is no playerID event field).
    return dict(type='combatantinfo', fight=11, sourceID=actor, specID=251,
                source=dict(id=actor, name=f'Player{actor}', server='BurningLegion',
                            type='Player', subType='DeathKnight'), critMelee=1410)


class Response:
    def __init__(self, payload=None, status=200):
        self.status_code, self.headers, self.payload = status, {}, payload

    def json(self):
        return self.payload


def page(events, cursor=None, extra_actors=()):
    actors, raw = list(extra_actors), []
    for original in events:
        payload = deepcopy(original)
        if 'source' in payload:
            actors.append(payload.pop('source'))
        raw.append(payload)
    return Response({'data': {'reportData': {'report': {
        'masterData': {'actors': actors},
        'events': {'data': raw, 'nextPageTimestamp': cursor}}}}})


@override_settings(WCL_V2_CONFIG={'client_id': 'completeness-client', 'client_secret': 'not-real'})
class WclInputCompletenessTests(TransactionTestCase):
    def setUp(self):
        now = timezone.now()
        WclApiBudget.objects.create(account_key=account_key('completeness-client'),
            limit_per_hour=18000, spent=0, reset_at=now + timedelta(hours=1), observed_at=now)

    def collect(self, rows, responses):
        fetcher = SpecDetailBase(None, None)
        fetcher._wcl_token, fetcher._wcl_token_expire = 'not-real', float('inf')
        with patch('requests.post', side_effect=responses) as http:
            result = collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual(result['selected'], sum(result[key] for key in (
            'cached', 'matched', 'missing', 'permission_denied', 'failed', 'pending')))
        return result, fetcher, http

    def test_bare_http_403_does_not_poison_report_and_next_call_recovers(self):
        result, fetcher, http = self.collect([sample()], [Response(status=403)])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(fetcher._wcl_last_error, 'http_403')
        self.assertEqual((result['failed'], result['permission_denied']), (1, 0))
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        result, _, http = self.collect([sample()], [page([event()])])
        self.assertEqual(http.call_count, 1)
        self.assertEqual((result['matched'], result['negative_cached']), (1, 0))
        self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, event())

    def test_bare_http_403_aborts_remaining_reports_without_negative_cache(self):
        other = {**sample(), 'report_code': 'Other'}
        result, fetcher, http = self.collect([sample(), other],
                                            [Response(status=403), page([event()])])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(fetcher._wcl_last_error, 'http_403')
        self.assertEqual((result['failed'], result['matched'], result['permission_denied']), (2, 0, 0))
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertFalse(WclCombatantSnapshot.objects.exists())

    def test_only_report_scoped_graphql_permission_creates_report_negative(self):
        for path in (None, ['worldData', 'encounter'], ['reportData', 'other']):
            with self.subTest(path=path):
                denied = Response({'errors': [{'message': 'You do not have permission to view this report.',
                                              'path': path}]})
                result, _, _ = self.collect([sample()], [denied])
                self.assertEqual(result['failed'], 1)
                self.assertFalse(WclCombatantFetchState.objects.exists())
        denied = Response({'errors': [{'message': 'You do not have permission to view this report.',
                                      'path': ['reportData', 'report']}]})
        result, _, _ = self.collect([sample()], [denied])
        self.assertEqual(result['permission_denied'], 1)
        state = WclCombatantFetchState.objects.get()
        self.assertEqual((state.report_code, state.fight_id, state.status), ('Report', 0, 'permission_denied'))
        self.assertEqual(state.retry_after - state.checked_at, timedelta(hours=1))
        result, _, http = self.collect([sample()], [])
        http.assert_not_called()
        self.assertEqual(result['negative_cached'], 1)

    def test_incomplete_second_page_is_atomic_preserves_old_and_can_retry(self):
        old = WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11,
                                                 actor_id=232, payload_json=event())
        # Every invalid input must fail before immutable inserts or missing TTLs.
        cases = [('source', key, value) for key, values in (
            ('name', [123, [], {}]),
            ('server', [{'name': 'BurningLegion'}]),
            ('id', [None, 0, 999])) for value in values]
        cases += [('event', key, value) for key, values in (
            ('specID', [True, False, -1, '0', 'invalid', {}, []]),
            ('sourceID', [None, 0, 999]),
            ('fight', [None, 0, 12])) for value in values]
        for container, key, value in cases:
            with self.subTest(container=container, field=key, value=value):
                # Keep regression failures independent instead of letting one
                # poisoned negative cache mask the remaining invalid inputs.
                WclCombatantFetchState.objects.all().delete()
                WclCombatantSnapshot.objects.exclude(pk=old.pk).delete()
                malformed = event(234)
                target = malformed['source'] if container == 'source' else malformed
                if value is None:
                    target.pop(key)
                else:
                    target[key] = value
                result, _, http = self.collect([sample(), sample(233), sample(234)],
                    [page([event(233)], 200), page([malformed])])
                self.assertEqual(http.call_count, 2)
                self.assertEqual([call.kwargs['json']['variables']['start'] for call in http.call_args_list],
                                 [None, 200])
                self.assertIn('actors(type: "Player")', http.call_args.kwargs['json']['query'])
                self.assertEqual((result['cached'], result['failed'], result['missing'],
                                  result['snapshots_written']), (1, 2, 0, 0))
                self.assertFalse(WclCombatantFetchState.objects.exists())
                self.assertEqual(WclCombatantSnapshot.objects.count(), 1)
                old.refresh_from_db()
                self.assertEqual(old.payload_json, event())
                # No poisoned get_or_create key or six-hour wait after bad input.
                recovered, _, http = self.collect([sample(), sample(233), sample(234)],
                    [page([event(233)], 200), page([event(234)])])
                self.assertEqual(http.call_count, 2)
                self.assertEqual((recovered['cached'], recovered['matched'], recovered['negative_cached'],
                                  recovered['snapshots_written']), (1, 2, 0, 2))
                self.assertEqual(WclCombatantSnapshot.objects.get(actor_id=234).payload_json, event(234))
                self.assertFalse(WclCombatantFetchState.objects.exists())
                WclCombatantSnapshot.objects.exclude(pk=old.pk).delete()

    def test_complete_missing_player_fields_skip_only_that_actor_after_all_pages(self):
        cases = [('source', key, value) for key, values in (
            ('name', [None, '', '  ', 'Anonymous', 'MISSING']),
            ('server', [None, '', ' - ', 'MISSING'])) for value in values]
        cases += [('event', 'specID', value) for value in (None, '', 0, 'MISSING')]
        for container, key, value in cases:
            with self.subTest(container=container, field=key, value=value):
                WclCombatantSnapshot.objects.all().delete()
                WclCombatantFetchState.objects.all().delete()
                missing = event(234)
                # Unknown spec is unavailable even if other fields have data.
                missing['talentTree'] = [{'id': 777, 'rank': 1}]
                target = missing['source'] if container == 'source' else missing
                if value == 'MISSING':
                    target.pop(key)
                else:
                    target[key] = value
                result, _, http = self.collect([sample(233), sample(234)],
                    [page([event(233)], 200), page([missing])])
                self.assertEqual(http.call_count, 2)
                self.assertEqual((result['matched'], result['missing'], result['failed'],
                                  result['snapshots_written']), (1, 1, 0, 1))
                self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, event(233))
                state = WclCombatantFetchState.objects.get()
                self.assertEqual((state.report_code, state.fight_id, state.status), ('Report', 11, 'missing'))
                self.assertEqual(state.retry_after - state.checked_at, timedelta(hours=6))
                # Exact positives win over this fight's negative for other players.
                repeated, _, http = self.collect([sample(233), sample(234)], [])
                http.assert_not_called()
                self.assertEqual((repeated['cached'], repeated['missing'], repeated['negative_cached']), (1, 1, 1))

    def test_partial_stats_and_talents_keep_independent_field_denominators(self):
        from botend.services.wcl_combatant_snapshot import enrich_dungeon_records
        from botend.services.spec_stats_service import SpecStatsService
        partial = event(233)
        partial.pop('critMelee')
        partial.update(hasteMelee=600, talentTree=[{'id': 777, 'rank': 1}], gear=[{'id': 456}])
        no_talents = {**event(234), 'hasteMelee': 400, 'gear': [{'id': 789}], 'talentTree': None}
        rows = [sample(233), sample(234)]
        result, _, _ = self.collect(rows, [page([partial, no_talents])])
        self.assertEqual((result['matched'], result['failed'], result['snapshots_written']), (2, 0, 2))
        records = enrich_dungeon_records(rows, 'DeathKnight', 'Frost')
        self.assertEqual(records[0]['stats_json'], {'haste': {'rating': 600}})
        self.assertEqual(records[0]['talents_json'][0]['node_id'], 777)
        self.assertEqual(records[0]['gear_json'][0]['id'], 456)
        self.assertFalse(records[1].get('talents_json'))
        self.assertEqual(records[1]['gear_json'][0]['id'], 789)
        stats = SpecStatsService._compute_dungeon_records(rows, 1, 101, 'Dungeon', 'DeathKnight', 'Frost')
        secondary = {row['key']: row for row in stats['secondary_stats']}
        self.assertEqual((secondary['crit']['sample_size'], secondary['crit']['rating']['avg']), (1, 1410))
        self.assertEqual((secondary['haste']['sample_size'], secondary['haste']['rating']['avg']), (2, 500))
        self.assertIsNone(secondary['haste']['pct'])

    def test_missing_player_is_not_a_license_to_ignore_corrupt_batch(self):
        missing = {**event(234), 'specID': 0}
        conflict = {**missing, 'critMelee': 999}
        no_source = {key: value for key, value in missing.items() if key != 'source'}
        untyped = {**missing, 'source': {**missing['source'], 'type': None}}
        broken_page = page([missing])
        broken_page.payload['data']['reportData']['report']['events'].pop('nextPageTimestamp')
        no_master = page([missing])
        no_master.payload['data']['reportData']['report'].pop('masterData')
        bad_container = page([missing])
        bad_container.payload['data']['reportData']['report']['events']['data'] = {}
        bad_event = page([missing])
        bad_event.payload['data']['reportData']['report']['events']['data'] = [None]
        for response in (page([no_source]), page([untyped]), page([missing, conflict]),
                         broken_page, no_master, bad_container, bad_event, Response(status=500)):
            with self.subTest(response=response.payload):
                result, _, _ = self.collect([sample(233), sample(234)],
                    [page([event(233)], 200), response, response, response])
                self.assertEqual((result['failed'], result['missing'], result['snapshots_written']), (2, 0, 0))
                self.assertFalse(WclCombatantSnapshot.objects.exists())
                self.assertFalse(WclCombatantFetchState.objects.exists())

    def test_unrelated_actor_metadata_and_explicit_npc_do_not_block_players(self):
        # Unreferenced metadata is not a candidate fact, even if incomplete.
        # An explicitly identified NPC is not a player; do not invent its realm/spec.
        npc = dict(type='combatantinfo', fight=11, sourceID=900,
                   source={'id': 900, 'type': 'NPC'})
        result, _, _ = self.collect([sample()], [page([event(), npc], extra_actors=[{'id': 901}])])
        self.assertEqual((result['matched'], result['failed'], result['snapshots_written']), (1, 0, 1))
        self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, event())
        self.assertFalse(WclCombatantFetchState.objects.exists())
