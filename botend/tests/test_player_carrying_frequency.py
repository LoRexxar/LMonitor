"""Stable carrying counts verified independent events, not co-runs or people."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from botend.services.player_observation_behavior import build_behavior_review
from botend.services.player_observation_questions import four_plus_one

NOW = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
SEASON = 'season-mn-2'


def frequency_args(count=6, *, one_guest=False, split_core=False, unknown=0):
    keys = [f'{i:064x}' for i in range(1, 9)]
    subject, a, b, c, x, y, d, e = keys
    args = dict(subject=subject, best=[], recent=[], runs={}, rosters={}, scores={},
                score_times={}, histories={}, fetched_at=NOW, now=NOW,
                rio_season=SEASON, dungeon_ids=[])
    for i, key in enumerate(keys):
        prior = dict(run_id=100+i, dungeon_id=1, level=25, score=600,
                     timed=True, completed_at=(NOW-timedelta(days=2)).isoformat())
        if key not in (x, y):
            args['scores'][key] = [prior]
            args['score_times'][key] = NOW
        else:
            prior.update(level=15, score=400)
            args['histories'][key] = {'1': dict(ui=dict(season=SEASON, characterId=i+1,
                dungeonId=1, role='all', mode='scored', affixes='all', date='all'),
                fetched_at=NOW.isoformat(), coverage='observed_partial', status='ready',
                source_url='https://raider.io/api/characters/mythic-plus-runs', runs=[prior])}
    for i in range(count+unknown):
        rid = 1000+i
        completed = NOW-timedelta(hours=3)+timedelta(minutes=10*i)
        row = dict(url=f'https://raider.io/mythic-plus-runs/{SEASON}/{rid}', dungeon='Test',
            zone_id=1, mythic_level=20, score=500+i, completed_at=completed.isoformat(),
            clear_time_ms=300000, num_keystone_upgrades=1)
        args['recent'].append(row)
        args['runs'][rid] = row
        if i >= count:
            continue
        core = [subject, a, d, e] if split_core and i >= count//2 else [subject, a, b, c]
        guest = x if one_guest or i % 2 == 0 else y
        members = [(key, {'character': {'name': key[-2:], 'id': keys.index(key)+1}})
                   for key in core+[guest]]
        args['rosters'][rid] = (dict(dungeon={'id': 1}, mythic_level=20, num_chests=1,
            completed_at=completed.isoformat(), clear_time_ms=300000), members)
        args['histories'][guest]['1']['runs'].append(dict(run_id=rid, dungeon_id=1,
            level=20, score=500+i, timed=True, completed_at=completed.isoformat()))
    args['recent'].reverse()
    return args


class CarryingFrequencyTests(TestCase):
    def test_five_partial_six_strong_and_complete_event_count(self):
        for count, level in ((1, 'insufficient'), (2, 'insufficient'), (5, 'partial'), (6, 'strong'), (9, 'strong')):
            with self.subTest(count=count):
                result = build_behavior_review(**frequency_args(count))
                self.assertEqual(result['evidence_strengths']['carrying']['level'], level)
                self.assertEqual('carrying' in result['supported'], count >= 6)
                if count >= 3:
                    self.assertEqual(result['evidence_strengths']['carrying']['verified_runs'], count)
                    self.assertEqual(len(result['witnesses']['carrying']), count)

    def test_duplicate_rows_do_not_inflate_independent_runs(self):
        args = frequency_args(5)
        args['recent'] *= 3
        for history in args['histories'].values():
            history['1']['runs'] *= 3
        result = build_behavior_review(**args)
        grade = result['evidence_strengths']['carrying']
        self.assertEqual(grade['verified_runs'], 5)
        self.assertNotIn('carrying', result['supported'])

    def test_one_beneficiary_and_two_separate_cores_do_not_combine(self):
        for changes in ({'one_guest': True}, {'split_core': True}):
            with self.subTest(changes=changes):
                result = build_behavior_review(**frequency_args(6, **changes))
                self.assertNotIn('carrying', result['supported'])
                self.assertNotEqual(result['evidence_strengths']['carrying']['level'], 'strong')
        result = build_behavior_review(**frequency_args(6, split_core=True))
        self.assertEqual(result['evidence_strengths']['carrying']['verified_runs'], 3)

    def test_known_prior_score_is_counter_not_beneficiary(self):
        args = frequency_args(6)
        for ds in args['histories'].values():
            ds['1']['runs'][0].update(score=610, level=26)
        result = build_behavior_review(**args)
        self.assertNotIn('carrying', result['supported'])
        self.assertEqual(result['evidence_strengths']['carrying']['verified_runs'], 0)
        self.assertEqual(result['evidence_strengths']['carrying']['level'], 'excluded')

    def test_small_sample_and_unknown_rosters_keep_real_denominator(self):
        result = build_behavior_review(**frequency_args(6, unknown=2))
        grade = result['evidence_strengths']['carrying']
        freq = grade['frequency']
        self.assertEqual((freq['window_days'], freq['sample_limit'], freq['min_verified_runs']), (14, 20, 6))
        self.assertEqual((freq['observed_runs'], freq['verified_runs'], freq['unknown_runs']), (8, 6, 2))
        self.assertEqual(freq['assessed_runs']+freq['unknown_runs'], freq['observed_runs'])
        self.assertEqual(result['label_reviews']['carrying']['unknown_run_ids'], [1006, 1007])
        self.assertEqual(result['label_reviews']['carrying']['frequency'], freq)

    def test_private_empty_stale_and_missing_histories_are_unknown_not_zero(self):
        for change in ('private', 'empty', 'stale', 'missing'):
            with self.subTest(change=change):
                args = frequency_args(6)
                key = f'{6:064x}'
                history = args['histories'][key]['1']
                if change == 'private':
                    history['status'] = 'private'
                elif change == 'empty':
                    history['runs'] = []
                elif change == 'stale':
                    history['fetched_at'] = (NOW-timedelta(days=3)).isoformat()
                else:
                    args['histories'].pop(key)
                result = build_behavior_review(**args)
                grade = result['evidence_strengths']['carrying']
                self.assertNotIn('carrying', result['supported'])
                self.assertEqual(grade['level'], 'partial')
                self.assertEqual(grade['frequency']['observed_runs'], 6)
                self.assertEqual(grade['frequency']['unknown_runs'], 3)

    def test_unknown_core_history_does_not_turn_into_exclusion(self):
        args = frequency_args(6)
        args['scores'].clear()
        result = build_behavior_review(**args)
        grade = result['evidence_strengths']['carrying']
        self.assertNotEqual(grade['level'], 'excluded')
        self.assertEqual(grade['frequency']['unknown_runs'], 6)
        self.assertTrue(any(q['purpose'] == 'verify_core_history' for q in result['questions']))

    def test_current_selected_scope_not_best_or_old_or_extra_events(self):
        args = frequency_args(6)
        args['recent'][0]['completed_at'] = (NOW-timedelta(days=15)).isoformat()
        result = build_behavior_review(**args)
        self.assertNotIn('carrying', result['supported'])
        self.assertEqual(result['evidence_strengths']['carrying']['frequency']['observed_runs'], 5)
        self.assertEqual(result['evidence_strengths']['carrying']['verified_runs'], 5)

    def test_two_beneficiaries_do_not_early_stop_other_dungeon_question(self):
        # Direct planning boundary: two verified people are not six verified runs.
        from botend.services.player_observation_behavior import _record
        args = frequency_args(6)
        teams = {rid: frozenset(k for k, _ in value[1]) for rid, value in args['rosters'].items()}
        run_records = {rid: _record(row, rid=rid, dungeon=1, profile=True) for rid, row in args['runs'].items()}
        records = {key: {r['run_id']: _record(r) for r in rows} for key, rows in args['scores'].items()}
        histories = deepcopy(args['histories'])
        for key, ds in histories.items():
            records[key] = {r['run_id']: _record(r) for r in ds['1']['runs']}
        for rid in range(1002, 1006):
            run_records[rid]['dungeon_id'] = 2
            args['rosters'][rid][0]['dungeon']['id'] = 2
        for key, rows in records.items():
            if key in args['scores']:
                old = deepcopy(next(iter(rows.values())))
                old.update(run_id=old['run_id']+100, dungeon_id=2)
                rows[old['run_id']] = old
        inquiry = four_plus_one(subject=args['subject'], selected=args['runs'], teams=teams,
            run_records=run_records, rosters=args['rosters'], records=records, histories=histories)
        self.assertEqual(len(inquiry['candidates']), 2)
        self.assertEqual(inquiry['events'], [])
        self.assertTrue(inquiry['questions'])
        self.assertEqual({q['dungeon_id'] for q in inquiry['questions']}, {2})
        self.assertEqual({q['member_key'] for q in inquiry['questions']}, {f'{5:064x}', f'{6:064x}'})

    def test_read_policy_rejects_old_collapsed_and_bad_metadata(self):
        from botend.services.player_observation_carrying_policy import stable_carrying_frequency
        result = build_behavior_review(**frequency_args(6))
        self.assertEqual(stable_carrying_frequency(result)['verified_runs'], 6)
        for mutated in ({'evidence_strengths': {'carrying': {'level': 'strong', 'verified_runs': 2}}},
                        build_behavior_review(**frequency_args(5))):
            self.assertIsNone(stable_carrying_frequency(mutated))
        changed = deepcopy(result)
        changed['carrying_frequency']['observed_runs'] = True
        self.assertIsNone(stable_carrying_frequency(changed))

    def test_captured_anuo_retains_all_real_events_without_stable_tag(self):
        from botend.tests.test_player_four_plus_one import anuo_args
        result = build_behavior_review(**anuo_args())
        grade = result['evidence_strengths']['carrying']
        self.assertEqual(grade['level'], 'partial')
        self.assertNotIn('carrying', result['supported'])
        self.assertGreater(grade['verified_runs'], 2)
        self.assertLess(grade['verified_runs'], 6)
        self.assertEqual(len(result['witnesses']['carrying']), grade['verified_runs'])
