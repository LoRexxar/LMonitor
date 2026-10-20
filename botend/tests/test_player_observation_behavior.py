"""Focused pure-contract regressions: no transport, ORM or combat inputs."""
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from botend.services.player_observation_behavior import build_behavior_review


class BehaviorReviewTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        self.keys = [format(i, '064x') for i in range(7)]
        self.args = dict(subject=self.keys[0], best=[], recent=[], runs={}, rosters={},
                         scores={}, score_times={}, fetched_at=self.now,
                         rio_season='season-mn-2', now=self.now, dungeon_ids=set(range(1, 9)))
        for i in range(3):
            self.add_run(100+i, 22, self.now-timedelta(hours=4-i), self.keys[:5])

    def add_run(self, rid, level, when, team, score=520, timed=True):
        row = dict(keystone_run_id=rid, url=f'https://raider.io/mythic-plus-runs/season-mn-2/{rid}',
                   dungeon='Dungeon', mythic_level=level, score=score,
                   num_keystone_upgrades=int(timed), completed_at=when.isoformat())
        self.args['runs'][rid] = row
        self.args['recent'].append(row)
        self.args['rosters'][rid] = (dict(keystone_run_id=rid, dungeon={'id': 1, 'slug': 'dungeon'},
            mythic_level=level, completed_at=when.isoformat(), clear_time_ms=1800000,
            num_chests=int(timed)), [(key, {}) for key in team])

    def records(self, key, level, *, all_dungeons=False, when=None):
        when = when or self.now-timedelta(days=2)
        rows = []
        for did in (range(1, 9) if all_dungeons else [1]):
            rows.append(dict(keystone_run_id=1000+int(key, 16)*100+did,
                dungeon={'id': did, 'slug': 'dungeon'}, mythic_level=level,
                score=365+15*(level-12), num_chests=1, completed_at=when.isoformat(), alternate=None))
        self.args['scores'][key] = rows
        self.args['score_times'][key] = self.now
        return rows

    def build(self, **changes):
        return build_behavior_review(**(self.args | changes))

    def test_structure_is_separate_and_comparison_groups_exclude_subject(self):
        out = self.build()
        self.assertEqual(out['structure']['recent_runs'], 3)
        self.assertIn('fixed_five', out['structure']['codes'])
        self.assertTrue(any(self.keys[0] not in g['members'] for g in out['structure']['comparison_groups']))
        self.assertFalse(out['supported'])
        self.assertEqual(set(out), {'version', 'triggered', 'structure', 'evidence', 'supported',
                                    'witnesses', 'resilience', 'questions', 'limitations'})
        json.dumps(out)

    def test_carrying_requires_two_independent_runs_and_all_next_level_before_start(self):
        self.records(self.keys[0], 23, all_dungeons=True)
        self.current_snapshot(self.keys[4])
        out = self.build()
        self.assertEqual(out['witnesses']['carrying'], [100, 101, 102])
        self.assertEqual(len(out['supported']['carrying']), 3)
        self.assertEqual(out['resilience']['events'][0]['members'][self.keys[0]]['state'], 'confirmed_before_start')
        single = self.build(recent=[self.args['recent'][0]]*3)
        self.assertNotIn('carrying', single['supported'])
        self.assertEqual(single['structure']['recent_runs'], 1)

    def test_all_same_level_and_unknown_dungeon_scope_do_not_prove_protection(self):
        self.records(self.keys[0], 22, all_dungeons=True)
        self.args['scores'][self.keys[0]][0]['mythic_level'] = 23
        self.args['scores'][self.keys[0]][0]['score'] = 530
        self.records(self.keys[4], 21)
        for kwargs in ({}, {'dungeon_ids': None}, {'rio_season': 'other-season'}):
            out = self.build(**kwargs)
            self.assertNotIn('carrying', out['supported'])
            if 'rio_season' not in kwargs:
                self.assertTrue(out['triggered'])

    def test_completion_during_run_and_missing_duration_are_unknown(self):
        rows = self.records(self.keys[0], 23, all_dungeons=True)
        rows[-1]['completed_at'] = (self.now-timedelta(hours=4, minutes=10)).isoformat()
        out = self.build()
        self.assertEqual(out['resilience']['events'][0]['members'][self.keys[0]]['state'], 'unknown')
        self.args['rosters'][101][0].pop('clear_time_ms')
        out = self.build()
        self.assertIsNone(out['resilience']['events'][1]['started_at'])
        self.assertEqual(out['resilience']['events'][1]['members'][self.keys[0]]['state'], 'unknown')

    def test_unknown_teammates_and_future_records_never_support_carrying(self):
        self.records(self.keys[0], 23, all_dungeons=True)
        out = self.build()
        self.assertNotIn('carrying', out['supported'])
        self.assertTrue(out['questions'])
        self.records(self.keys[4], 21)
        self.records(self.keys[0], 23, all_dungeons=True, when=self.now-timedelta(hours=1))
        self.assertNotIn('carrying', self.build()['supported'])

    def test_recovery_unknown_distinguishes_missing_comparison_from_protection(self):
        self.records(self.keys[0], 23, all_dungeons=True)
        confirmed = self.build()
        self.assertEqual(confirmed['resilience']['subject_key'], self.keys[0])
        texts = [e['text'] for e in confirmed['evidence'] if ':key_recovery_unknown:' in e['id']]
        self.assertEqual(len(texts), 3)
        for text in texts:
            self.assertIn('全23跑前保护已证实', text)
            self.assertIn('缺可比受益成员', text)
            self.assertNotIn('保护未完整证实', text)
        self.args['scores'][self.keys[0]].pop()
        unknown = self.build()
        texts = [e['text'] for e in unknown['evidence'] if ':key_recovery_unknown:' in e['id']]
        self.assertEqual(len(texts), 3)
        self.assertTrue(all('保护未知' in text for text in texts))
        self.assertNotIn('carrying', confirmed['supported'])
        self.assertNotIn('carrying', unknown['supported'])

    def test_same_level_decimal_boundary_is_not_downlevel(self):
        rows = self.records(self.keys[0], 22, all_dungeons=True)
        rows[0]['score'] = 520.2
        self.records(self.keys[4], 21)
        out = self.build()
        self.assertNotIn('carrying', out['supported'])
        self.assertFalse(out['triggered'])

    def test_being_carried_requires_three_previously_higher_protected_peers(self):
        self.current_snapshot(self.keys[0])
        for key in self.keys[1:4]:
            self.records(key, 23, all_dungeons=True)
        self.assertIn('being_carried', self.build()['supported'])
        self.args['scores'].pop(self.keys[3])
        self.assertNotIn('being_carried', self.build()['supported'])

    def test_joint_progress_joins_shared_prior_run_before_compression(self):
        for key in self.keys[:4]:
            rows = self.records(key, 21)
            rows[0]['keystone_run_id'] = 900
            rows[0]['alternate'] = dict(rows[0], keystone_run_id=901, mythic_level=24, score=545,
                completed_at=(self.now-timedelta(days=1)).isoformat(), alternate=None)
        out = self.build()
        self.assertIn('joint_progress', out['supported'])
        text = ' '.join(e['text'] for e in out['evidence'] if ':joint_progress:' in e['id'])
        self.assertIn('已知无提升', text)
        for i, key in enumerate(self.keys[:4]):
            self.args['scores'][key][0]['keystone_run_id'] = 900+i
        self.assertNotIn('joint_progress', self.build()['supported'])

    def test_recent_denominator_includes_untimed_but_not_best_or_history(self):
        self.args['recent'][0]['num_keystone_upgrades'] = 0
        self.args['rosters'][100][0]['num_chests'] = 0
        for i in range(3, 12):
            self.add_run(100+i, 22, self.now-timedelta(hours=15-i), self.keys[:5])
        out = self.build(best=[self.args['recent'][-1]])
        self.assertEqual(out['structure']['recent_runs'], 10)
        self.assertEqual(out['structure']['untimed_runs'], 1)
        self.assertEqual(out['structure']['roster_runs'], 10)

    def test_rotations_distinguish_single_change_and_occasional_substitution(self):
        self.add_run(103, 22, self.now-timedelta(hours=1), self.keys[:4]+[self.keys[5]])
        self.assertIn('occasional_substitution', self.build()['structure']['codes'])
        self.add_run(104, 22, self.now-timedelta(minutes=40), self.keys[:4]+[self.keys[5]])
        self.assertNotIn('rotating_core', self.build()['structure']['codes'])
        self.add_run(105, 22, self.now-timedelta(minutes=10), self.keys[:5])
        self.assertIn('rotating_core', self.build()['structure']['codes'])

    def test_history_scope_and_immutability(self):
        self.records(self.keys[0], 23, all_dungeons=True)
        self.current_snapshot(self.keys[4])
        old = self.args['scores'][self.keys[0]].pop()
        hist = dict(fetched_at=self.now.isoformat(), source_url='https://raider.io/api/characters/mythic-plus-runs',
            ui=dict(season='season-mn-2', characterId=77, role='all', mode='scored', affixes='all', date='all', dungeonId=8),
            runs=[dict(run_id=old['keystone_run_id'], dungeon_id=8, level=23, score=530,
                       timed=True, completed_at=old['completed_at'])])
        self.args['histories'] = {self.keys[0]: {'8': hist}}
        before = copy.deepcopy(self.args)
        self.assertIn('carrying', self.build()['supported'])
        self.assertEqual(self.args, before)
        hist['ui']['mode'] = 'all'
        self.assertNotIn('carrying', self.build()['supported'])

    def current_snapshot(self, key):
        row = self.args['runs'][102]
        self.args['scores'][key] = [dict(keystone_run_id=102, dungeon_id=1,
            level=22, score=row['score'], timed=True, completed_at=row['completed_at'])]
        self.args['score_times'][key] = self.now

    def test_relative_current_run_snapshot_and_reverse(self):
        self.records(self.keys[0], 23, all_dungeons=True)
        for key in self.keys[1:5]:
            self.current_snapshot(key)
        out = self.build()
        self.assertIn('carrying', out['supported'])
        self.assertIn('high_score_collaborator', out['supported'])
        self.assertIn('快照', ' '.join(e['text'] for e in out['evidence']))
        self.current_snapshot(self.keys[0])
        for key in self.keys[1:4]:
            self.records(key, 23, all_dungeons=True)
        self.assertIn('being_carried', self.build()['supported'])
        self.args['score_times'][self.keys[0]] = self.now-timedelta(hours=49)
        self.assertNotIn('being_carried', self.build()['supported'])

    def test_lower_early_record_is_not_personal_ceiling(self):
        self.records(self.keys[0], 23, all_dungeons=True)
        self.records(self.keys[4], 21)
        self.assertNotIn('carrying', self.build()['supported'])

    def test_resilience_gap_questions_and_invalid_history(self):
        self.records(self.keys[0], 23, all_dungeons=True)
        old = self.args['scores'][self.keys[0]].pop()
        self.current_snapshot(self.keys[4])
        out = self.build()
        question = next(q for q in out['questions'] if q.get('purpose') == 'resilience_gap')
        self.assertEqual((question['dungeon_id'], question['required_level']), (8, 23))
        self.assertEqual(question['run_ids'], [100, 101, 102])
        self.assertLessEqual(len(out['questions']), 4)
        hist = dict(fetched_at=self.now.isoformat(), source_url='https://raider.io/api/characters/mythic-plus-runs',
            ui=dict(season='season-mn-2', characterId=77, role='all', mode='scored', affixes='all', date='all', dungeonId=8),
            runs=[dict(run_id=old['keystone_run_id'], dungeon_id=8, level=23, score=530,
                       timed=True, completed_at=old['completed_at'])])
        for extra in ({'private': True}, {'status': 'private'}, {'status': 'error'},
                      {'status': 'fetching'}, {'last_error': 'failed'},
                      {'fetched_at': (self.now-timedelta(hours=49)).isoformat()}):
            with self.subTest(extra=extra):
                out = self.build(histories={self.keys[0]: {8: hist | extra}})
                self.assertNotIn('carrying', out['supported'])
                self.assertTrue(any(q.get('purpose') == 'resilience_gap' for q in out['questions']))
        out = self.build(histories={self.keys[0]: {8: hist}})
        self.assertIn('carrying', out['supported'])
        self.assertFalse(any(q.get('purpose') == 'resilience_gap' for q in out['questions']))
        out = self.build(histories={self.keys[0]: {8: hist | {'runs': []}}})
        self.assertNotIn('carrying', out['supported'])
        self.assertFalse(any(q.get('purpose') == 'resilience_gap' for q in out['questions']))

    def test_stale_recent_has_no_publishable_behavior(self):
        out = self.build(fetched_at=self.now-timedelta(days=3))
        self.assertFalse(out['triggered'])
        self.assertFalse(out['supported'])
