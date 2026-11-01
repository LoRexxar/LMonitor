"""真实公开记录回放及普通固定队、正常替换成员的反例。"""
import copy
import json
from pathlib import Path
from datetime import datetime, timezone
from unittest import TestCase

from botend.services.player_observation_behavior import build_behavior_review
from botend.services.player_observation_questions import roster_questions
from botend.services.player_observation import _member_identity, _merge_behavior_investigation
from botend.services.player_observation_investigation import build_investigation


def anuo_args():
    data = json.loads((Path(__file__).parent/'fixtures/player_four_plus_one_anuo.json').read_text(encoding='utf-8'))
    runs = {int(r['url'].split('/')[5].split('-')[0]):r for r in data['best']+data['recent']}
    rosters = {int(rid):(raw,[(_member_identity(m),m) for m in raw['roster']])
               for rid,raw in data['rosters'].items() if int(rid) in runs}
    return dict(subject=data['subject'],best=data['best'],recent=data['recent'],runs=runs,rosters=rosters,
        scores={k:v['dungeons'] for k,v in data['scores'].items()},
        score_times={k:datetime.fromisoformat(v['fetched_at']) for k,v in data['scores'].items()},
        histories=data['histories'],rio_season='season-mn-2',
        fetched_at=datetime(2026,10,20,12,56,tzinfo=timezone.utc),
        now=datetime(2026,10,20,14,56,16,tzinfo=timezone.utc),dungeon_ids=[])


class FourPlusOneTests(TestCase):
    def test_anuo_two_beneficiaries_are_real_partial_events_not_stable_mode(self):
        args=anuo_args(); result=build_behavior_review(**args)
        self.assertNotIn('carrying', result['supported'])
        self.assertEqual(result['witnesses']['carrying'], [21545839, 21551218, 21654678])
        self.assertTrue(any('carrying' in q.get('label_codes', []) for q in result['questions']))
        grade = result['evidence_strengths']['carrying']
        self.assertEqual((grade['level'], grade['confidence']), ('partial', '中'))
        self.assertEqual(grade['distinct_beneficiaries'], 2)
        self.assertGreaterEqual(grade['core_runs'], 3)
        self.assertIn('是否收费或存在交易', result['context_unknowns'])

    def test_one_verified_beneficiary_is_partial_and_does_not_publish_carrying(self):
        args = anuo_args()
        args['histories'].pop('620e5a03673b8c031b2708439ff9da0bfef5286bfb438593fedad851c368939b')
        result = build_behavior_review(**args)
        self.assertEqual(result['evidence_strengths']['carrying']['level'], 'partial')
        self.assertNotIn('carrying', result['supported'])

    def test_pending_guest_gain_is_limited_not_strong(self):
        args = anuo_args()
        args['histories'] = {}
        result = build_behavior_review(**args)
        self.assertTrue(result['questions'])
        self.assertEqual(result['evidence_strengths']['carrying']['level'], 'limited')
        self.assertNotIn('carrying', result['supported'])

    def test_same_level_core_attainment_is_sufficient(self):
        args=anuo_args()
        args['recent']=[r for r in args['recent'] if int(r['url'].split('/')[5])!=21545839]
        result=build_behavior_review(**args)
        self.assertIn(21551218,result['witnesses']['carrying'])
        self.assertNotIn('carrying',result['supported'])

    def test_guest_untimed_higher_score_also_counters_an_increase(self):
        args=anuo_args()
        key='620e5a03673b8c031b2708439ff9da0bfef5286bfb438593fedad851c368939b'
        for run in args['histories'][key]['16359']['runs']:
            if run['run_id']==21627600:
                run.update(level=27,score=540,timed=False)
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertFalse(any(q['member_key']==key and q['dungeon_id']==16359
                             and 'carrying' in q.get('label_codes', []) for q in result['questions']))

    def test_fixed_five_and_single_guest_multiple_runs_are_not_rotation(self):
        args=anuo_args()
        ids={21631117,21646965,21654678,21663568}
        args['recent']=[r for r in args['recent'] if int(r['url'].split('/')[5]) in ids]
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertFalse(any('carrying' in q.get('label_codes', []) for q in result['questions']))
        self.assertEqual(result['evidence_strengths']['carrying']['level'], 'excluded')

    def test_normal_substitutes_with_prior_ability_are_not_beneficiaries(self):
        args=anuo_args()
        guests=['52affcc3c4c1a22d15de2f13abcdf59b2eb5d369272475c0a9919f090bacbefc',
                '620e5a03673b8c031b2708439ff9da0bfef5286bfb438593fedad851c368939b']
        selected=set(args['runs'])
        for key in guests:
            for history in args['histories'][key].values():
                for run in history['runs']:
                    if run['run_id'] not in selected and run['timed']:
                        run['level']=24;run['score']=545
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        carrying = [q for q in result['questions'] if 'carrying' in q.get('label_codes', [])]
        self.assertTrue(carrying)
        # 同成员其他副本仍有具体未知事件；不把已核本当作全历史排除。
        self.assertFalse(any((q['member_key'], str(q['dungeon_id'])) in
            {(key, did) for key in guests for did in args['histories'][key]} for q in carrying))
        self.assertEqual(result['evidence_strengths']['carrying']['level'], 'limited')

    def test_incomplete_rosters_cannot_exclude_the_pattern(self):
        args = anuo_args()
        ids = {21631117, 21646965, 21654678, 21663568}
        args['recent'] = [r for r in args['recent'] if int(r['url'].split('/')[5]) in ids]
        args['rosters'].pop(21663568)
        result = build_behavior_review(**args)
        self.assertNotEqual(result['evidence_strengths']['carrying']['level'], 'excluded')

    def test_missing_core_history_creates_bounded_same_dungeon_questions(self):
        args = anuo_args()
        args['scores'] = {}; args['histories'] = {}
        # 移除较早阵容中的跑前能力，只保留后续重复组合。
        ids = {21551218, 21631117, 21646965, 21654678, 21663568}
        args['recent'] = [r for r in args['recent'] if int(r['url'].split('/')[5]) in ids]
        args['rosters'] = {rid: value for rid, value in args['rosters'].items() if rid in ids}
        result = build_behavior_review(**args)
        questions = [q for q in result['questions'] if q['purpose'] == 'verify_core_history']
        self.assertTrue(questions)
        self.assertEqual(len({(q['member_key'], q['dungeon_id']) for q in questions}), len(questions))
        for question in questions:
            self.assertIn(question['member_key'], question['core_members'])
            self.assertEqual(question['dungeon_id'], args['rosters'][question['run_ids'][0]][0]['dungeon']['id'])
        self.assertNotIn('carrying', result['supported'])
        self.assertNotEqual(result['evidence_strengths']['carrying']['level'], 'excluded')

    def test_exclusion_is_rendered_as_a_scoped_negative_not_a_positive_label(self):
        from botend.services.player_observation_contract import build_contract, render_investigation
        args = anuo_args()
        ids = {21631117, 21646965, 21654678, 21663568}
        args['recent'] = [r for r in args['recent'] if int(r['url'].split('/')[5]) in ids]
        review = build_behavior_review(**args)
        plan = _merge_behavior_investigation({'triggered': True, 'evidence': [], 'questions': [],
            'hypotheses': [], 'scope': '本轮样本'}, review)
        evidence = {'prompt_version': 9, 'evidence': review['evidence'] + plan['evidence'],
            'metrics': {'behavior_review': review, 'investigation': plan}}
        contract = build_contract(evidence)
        excluded = next(h for h in contract['hypotheses'] if h['id'] == 'assistance')
        self.assertEqual(excluded['assessment_options'], ['weakened'])
        self.assertIn('assistance', contract['required_hypothesis_ids'])
        value = {'schema_version': 9, 'confidence': '分项评估', 'next_question_ids': [],
            'hypotheses': [dict(id=h['id'], assessment=h['assessment_options'][0],
                support_ids=h['support_ids'][:3], counter_ids=h['counter_ids'], unknown_ids=h['unknown_ids'][:3])
                for h in contract['hypotheses']]}
        rendered = render_investigation(value, evidence)
        result = next(h for h in rendered['hypotheses'] if h['id'] == 'assistance')
        self.assertEqual(result['assessment_label'], '已排除（本轮样本）')
        self.assertEqual(result['evidence_strength']['confidence'], '高')
        self.assertTrue(result['counter_evidence'])
        self.assertFalse(result['evidence'])

    def test_slot_order_does_not_define_the_rotating_member(self):
        args=anuo_args()
        for raw, members in args['rosters'].values():
            members.reverse()
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertEqual(result['witnesses']['carrying'], [21545839, 21551218, 21654678])

    def test_two_isolated_runs_are_insufficient_for_repeated_collaboration(self):
        args=anuo_args()
        args['recent']=[r for r in args['recent'] if int(r['url'].split('/')[5]) in {21551218,21654678}]
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertFalse(any('carrying' in q.get('label_codes', []) for q in result['questions']))

    def test_invalid_or_private_guest_history_never_supports_a_label(self):
        for change in ({'status':'private'},{'status':'error'},{'private':True},
                       {'fetched_at':'2026-10-22T00:00:00+00:00'}):
            with self.subTest(change=change):
                args=anuo_args()
                key='620e5a03673b8c031b2708439ff9da0bfef5286bfb438593fedad851c368939b'
                args['histories'][key]['16359'].update(change)
                self.assertNotIn('carrying',build_behavior_review(**args)['supported'])

    def test_missing_guest_history_creates_only_specific_event_questions(self):
        args=anuo_args(); args['histories']={}
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertTrue(result['questions'])
        for q in [q for q in result['questions'] if 'carrying' in q.get('label_codes', [])]:
            self.assertEqual(q['purpose'],'verify_guest_gain')
            self.assertEqual(len(q['core_members']),4)
            self.assertEqual(len(q['prior_run_ids']),4)
            self.assertTrue(q['reason']);self.assertTrue(q['expected_change'])
            self.assertEqual(q['dungeon_id'],args['rosters'][q['run_ids'][0]][0]['dungeon']['id'])

    def test_no_prior_guest_record_is_unknown_not_zero(self):
        args=anuo_args()
        for dungeons in args['histories'].values():
            for history in dungeons.values():
                history['runs']=[]
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])

    def test_roster_requests_are_tied_to_repeated_attainment(self):
        args=anuo_args()
        questions=roster_questions(subject=args['subject'],runs=args['runs'],visible={},season=args['rio_season'])
        self.assertIn(21654678,questions); self.assertIn(21764392,questions)
        for q in questions.values():
            self.assertTrue(q['prior_run_ids']);self.assertTrue(q['expected_change'])
        one={21654678:args['runs'][21654678]}
        self.assertEqual(roster_questions(subject=args['subject'],runs=one,visible={},season=args['rio_season']),{})


from unittest.mock import Mock, patch
from django.test import TestCase as DatabaseTestCase
from botend.models import SeasonMeta, PlayerSpecTopPlayer, RaiderIOCharacterActivity, RaiderIORunRoster
from botend.services import player_observation as service


class FourPlusOnePipelineTests(DatabaseTestCase):
    """回放真实缓存与补采接口；不连接生产数据库，不调用真实模型。"""
    def setUp(self):
        self.data = json.loads((Path(__file__).parent/'fixtures/player_four_plus_one_anuo.json').read_text(encoding='utf-8'))
        self.now = datetime(2026,10,20,14,56,16,tzinfo=timezone.utc)
        self.clock = patch('django.utils.timezone.now', return_value=self.now)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='测试赛季',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='cn',
            realm='shadowmourne', character_name='神奇的阿诺', class_name='DeathKnight', spec_name='Blood',
            rank=1, profile_url='https://raider.io/characters/cn/shadowmourne/神奇的阿诺')
        members = {}
        for rid, raw in self.data['rosters'].items():
            RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=int(rid),
                payload_json=raw, fetched_at=self.now, roster_visible=True, status='ready')
            members.update({_member_identity(m): m['character'] for m in raw['roster']})
        self.rows = {}
        for key, char in members.items():
            snapshot = self.data['scores'].get(key)
            history = copy.deepcopy(self.data['histories'].get(key, {}))
            # 第五人的历史从空缓存开始，以记录真实需要的两次补证。
            if char['id'] in (305720261, 309612162, 309531659):
                history = {}
            payload = {'mythic_plus_scored_history': history,
                'mythic_plus_recent_runs': self.data['cached_recent'].get(key, [])}
            if key == self.data['subject']:
                payload['mythic_plus_best_runs'] = self.data['best']
            row = RaiderIOCharacterActivity.objects.create(season=self.season,
                region=char['region']['slug'], realm_slug=char['realm']['slug'], character_name=char['name'],
                payload_json=payload, fetched_at=self.now, status='ready')
            if snapshot:
                row.scored_runs_json = {'ui': dict(season=self.season.rio_season, characterId=char['id'],
                    role='all', mode='scored', affixes='all', date='all'), 'dungeons': snapshot['dungeons']}
                row.scored_runs_fetched_at = datetime.fromisoformat(snapshot['fetched_at'])
                row.save()
            self.rows[key] = row
        self.activity = self.rows[self.data['subject']]
        self.calls = []

    def transport(self, url, **kwargs):
        self.calls.append((url,kwargs['params']))
        if url.endswith('/run-details'):
            return Mock(status_code=200,headers={},json=Mock(return_value=self.data['rosters'][str(kwargs['params']['id'])]))
        if url.endswith('/mythic-plus-scored-runs'):
            member = next(m for raw in self.data['rosters'].values() for m in raw['roster']
                          if m['character']['id'] == kwargs['params']['characterId'])
            snapshot = self.data['scores'].get(_member_identity(member), {})
            return Mock(status_code=200, headers={}, json=Mock(return_value={
                'ui': kwargs['params'], 'dungeons': snapshot.get('dungeons', [])}))
        self.assertTrue(url.endswith('/mythic-plus-runs'), (url,kwargs['params']))
        params = kwargs['params']
        history = next((h[str(params['dungeonId'])] for h in self.data['histories'].values()
            if str(params['dungeonId']) in h and h[str(params['dungeonId'])]['ui']['characterId'] == params['characterId']), {'runs': []})
        runs = [{'score': r['score'], 'summary': dict(season=params['season'],
            dungeon={'id':r['dungeon_id']}, keystone_run_id=r['run_id'], mythic_level=r['level'],
            num_chests=int(r['timed']), completed_at=r['completed_at'])} for r in history['runs']]
        return Mock(status_code=200, headers={}, json=Mock(return_value={'ui':params,'runs':runs}))

    def refresh(self):
        from botend.services.player_observation_sources import refresh_observation_sources
        with patch('botend.services.player_observation_sources._ActivityOnlyCollector.refresh',
                return_value={'status':'fresh','activity_id':self.activity.pk}), \
                patch('botend.services.player_observation_sources.requests.Session.get', side_effect=self.transport), \
                patch('botend.services.player_observation_sources._pace', return_value=True):
            return refresh_observation_sources(self.player, progressive=True, force=True)

    def finish_other_labels(self):
        """按已有同场范围补证；每轮预算固定，有限来源不能重复扫描。"""
        # The six-event policy can require another known event dungeon, unlike
        # the old producer's two-person early stop. Bound closure by the actual
        # known source domain, not a larger arbitrary retry/time allowance.
        allowed = set()
        for raw in self.data['rosters'].values():
            allowed.add(('run-details', None, None, raw['keystone_run_id']))
            for member in raw['roster']:
                cid = member['character']['id']
                allowed.add(('mythic-plus-scored-runs', cid, None, None))
                allowed.add(('mythic-plus-runs', cid, raw['dungeon']['id'], None))
        seen = set()
        for _ in range(len(allowed) + 1):
            self.calls.clear()
            result = self.refresh()
            self.assertLessEqual(result['followup_api_requests'], 2)
            for url, params in self.calls:
                signature = (url.rsplit('/', 1)[-1], params.get('characterId'), params.get('dungeonId'), params.get('id'))
                self.assertIn(signature, allowed, 'cannot expand beyond an already observed member/run/dungeon')
                self.assertNotIn(signature, seen)
                seen.add(signature)
            if result['api_requests'] == 0:
                return
        self.fail('已回答的补证问题未停止')

    def test_anuo_carrying_then_other_labels_share_bounded_budget(self):
        before = service.build_evidence(self.player)
        self.assertNotIn('carrying',before['metrics']['behavior_review']['supported'])
        result = self.refresh()
        self.assertEqual(result['basic_api_requests'],0)
        self.assertEqual(result['followup_api_requests'],2)
        self.assertEqual(result['investigation']['stop_reason'],'budget_completed')
        self.assertTrue(result['investigation']['remaining_question_ids'])
        self.assertEqual({(p['characterId'],p['dungeonId']) for _,p in self.calls},
                         {(305720261,9527),(309612162,16359)})
        for trace in result['investigation']['requests']:
            self.assertEqual(trace['question']['purpose'],'verify_guest_gain')
            self.assertTrue(trace['question']['reason']);self.assertTrue(trace['question']['expected_change'])
        after = service.build_evidence(self.player)
        self.assertNotIn('carrying',after['metrics']['behavior_review']['supported'])
        self.assertEqual(after['metrics']['behavior_review']['carrying_frequency']['verified_runs'], 2)
        self.assertTrue(any('carrying' in q.get('label_codes', []) for q in after['metrics']['behavior_review']['questions']))
        self.calls.clear()
        next_result = self.refresh()
        self.assertGreater(next_result['api_requests'], 0)
        self.assertLessEqual(next_result['api_requests'], 2)
        self.assertTrue(any(r['question']['purpose'] == 'verify_guest_gain' for r in next_result['investigation']['requests']))
        self.finish_other_labels()
        self.assertEqual(self.calls,[])
        from botend.tests.test_player_observation import structured_response
        def choose(prompt, **kwargs):
            data=json.loads(prompt)
            self.assertNotIn('carrying', [x['code'] for x in data['eligible_labels']])
            return json.dumps({'observations':[], 'conclusion_id':'public_sample',
                'investigation':structured_response(data['metrics']['investigation']['response_contract'])},ensure_ascii=False)
        engine=Mock(send_message=Mock(side_effect=choose))
        choose(json.dumps(service._generation_input(service.build_evidence(self.player))))
        self.assertEqual(service.generate_observation(self.player,engine=engine)['status'],'ready')
        view=service.project_player_observation(self.player)
        self.assertFalse(any(x['code'] == 'carrying' for x in view['observations']))
        self.assertTrue(view['has_investigation'])
        from django.template.loader import render_to_string
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': view})
        self.assertIn('未核实信息：', html)
        self.assertIn('是否收费或存在交易', html)
        # 模型不能把真实的少量协助线索提升成稳定强标签。
        invalid = json.loads(choose(json.dumps(service._generation_input(after))))
        invalid['observations'] = [dict(code='carrying', confidence='高', reason_id='repeated_witnesses',
            limitation_ids=['sample_scope'], evidence_ids=['behavior:carrying:21654678'])]
        with self.assertRaisesRegex(service.InvalidResult, 'invalid_label'):
            service._validate_publication(json.dumps(invalid), after, publication_version=service.PROMPT_VERSION)

    def test_replanning_stops_after_answer_withdraws_other_question(self):
        original = service.build_evidence
        calls = [0]
        def updated(player):
            evidence = original(player)
            if self.calls:
                evidence['metrics']['investigation']['questions']=[]
                evidence['metrics']['behavior_review']['questions']=[]
            calls[0]+=1
            return evidence
        with patch('botend.services.player_observation.build_evidence',side_effect=updated):
            result=self.refresh()
        self.assertEqual(len(self.calls),1)
        self.assertGreaterEqual(calls[0],2)
        self.assertTrue(any(q['status']=='not_needed' for q in result['investigation']['outcomes']))

    def test_missing_rosters_prefer_prior_attainment_and_complete_structure(self):
        # 复现线上起点：小花生的受益已缓存，两场黑按摩捣师阵容缺失。
        # 最新场次21663568只有同层跑前成绩；21654678有24层跑前证明，优先核验。
        guest = '52affcc3c4c1a22d15de2f13abcdf59b2eb5d369272475c0a9919f090bacbefc'
        row = self.rows[guest]
        row.payload_json['mythic_plus_scored_history'] = self.data['histories'][guest]
        row.save(update_fields=['payload_json'])
        for rid in (21663568, 21654678):
            RaiderIORunRoster.objects.filter(run_id=rid).update(payload_json={}, status='pending', fetched_at=None)
        self.assertNotIn('carrying', service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
        result = self.refresh()
        self.assertEqual([(url.rsplit('/', 1)[-1], params.get('id'), params.get('dungeonId'))
                          for url, params in self.calls[:3]],
                         [('run-details', 21654678, None), ('run-details', 21663568, None),
                          ('mythic-plus-runs', None, 16359)])
        # After roster repair, prefer the still-unchecked second beneficiary;
        # the already-checked guest's other dungeons must not take its place.
        self.assertEqual(self.calls[2][1]['characterId'], 309612162)
        self.assertEqual(result['api_requests'], 4)
        self.assertEqual(result['basic_api_requests'], 2)
        self.assertEqual(result['followup_api_requests'], 2)
        request = next(r for r in result['runs'] if r['status'] == 'ready')
        self.assertEqual(request['question']['prior_level_advantage'], 1)
        self.assertTrue(request['question']['selection_reason'])
        self.assertNotIn('carrying', service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
        self.assertTrue(result['investigation']['remaining_question_ids'])
        self.assertEqual(RaiderIORunRoster.objects.get(run_id=21663568).status, 'ready')
        self.finish_other_labels()
        self.assertEqual(self.calls, [])

    def test_normal_substitutes_can_check_joint_progress_without_carrying(self):
        guests = ('52affcc3c4c1a22d15de2f13abcdf59b2eb5d369272475c0a9919f090bacbefc',
                  '620e5a03673b8c031b2708439ff9da0bfef5286bfb438593fedad851c368939b')
        selected = {int(rid) for rid in self.data['rosters']}
        for key in guests:
            row = self.rows[key]
            histories = copy.deepcopy(self.data['histories'][key])
            for history in histories.values():
                for run in history['runs']:
                    if run['run_id'] not in selected and run['timed']:
                        run.update(level=24, score=545)
            row.payload_json['mythic_plus_scored_history'] = histories
            row.save(update_fields=['payload_json'])
        result = self.refresh()
        self.assertGreater(result['api_requests'], 0)
        self.assertLessEqual(result['api_requests'], 2)
        self.assertTrue(all(r['question']['dungeon_id'] not in (9527, 16359)
            for r in result['investigation']['requests'] if r['question']['purpose'] == 'verify_guest_gain'))
        evidence = service.build_evidence(self.player)
        self.assertNotIn('carrying', evidence['metrics']['behavior_review']['supported'])
        self.assertNotIn('carrying', [label['code'] for label in evidence['eligible_labels']])

    def test_roster_limit_zero_does_not_borrow_history_budget(self):
        from botend.services import player_observation_sources as sources
        for rid in (21663568, 21654678):
            RaiderIORunRoster.objects.filter(run_id=rid).update(payload_json={}, status='pending', fetched_at=None)
        with patch.object(sources._ActivityOnlyCollector, 'refresh',
                          return_value={'status': 'fresh', 'activity_id': self.activity.pk}), \
                patch.object(sources.requests.Session, 'get', side_effect=self.transport):
            result = sources.refresh_observation_sources(self.player, progressive=True, max_basic_roster_requests=0)
        self.assertEqual(result['basic_api_requests'], 0)
        self.assertFalse(any(url.endswith('/run-details') for url, _ in self.calls))
        self.assertNotIn('carrying', service.build_evidence(self.player)['metrics']['behavior_review']['supported'])

    def test_unrelated_missing_data_never_authorizes_request_even_force(self):
        with patch('botend.services.player_observation.build_evidence',return_value={
                'metrics':{'investigation':{'version':1,'triggered':False,'questions':[]}}}):
            result=self.refresh()
        self.assertEqual(result['api_requests'],0)
        self.assertEqual(self.calls,[])

    def test_partial_rosters_are_fetched_only_for_an_explicit_question(self):
        for rid in (21654678,21764392):
            RaiderIORunRoster.objects.filter(run_id=rid).update(payload_json={},status='pending',fetched_at=None)
        # 固定本轮目标为阵容核验，避免把历史接口的调用计入本断言。
        with patch('botend.services.player_observation.build_evidence',return_value={'metrics':{}}):
            result=self.refresh()
        self.assertGreater(result['basic_api_requests'],0)
        self.assertEqual(result['followup_api_requests'],0)
        for row in result['runs']:
            if row['status']=='ready':
                self.assertEqual(row['question']['purpose'],'verify_repeated_relationship')
                self.assertTrue(row['question']['prior_run_ids'])
                self.assertTrue(row['question']['expected_change'])

    def test_fixed_five_can_check_joint_history_without_carrying(self):
        ids={21631117,21646965,21654678,21663568}
        raw=copy.deepcopy(self.data['rosters']['21654678']['roster'])
        for row in RaiderIORunRoster.objects.all():
            row.payload_json['roster']=copy.deepcopy(raw)
            row.save(update_fields=['payload_json'])
        self.activity.payload_json['mythic_plus_recent_runs']=[r for r in self.data['recent']
            if int(r['url'].split('/')[5]) in ids]
        self.activity.save()
        evidence=service.build_evidence(self.player)
        self.assertNotIn('carrying',evidence['metrics']['behavior_review']['supported'])
        questions = evidence['metrics']['behavior_review']['questions']
        self.assertTrue(questions)
        self.assertTrue(all('carrying' not in q.get('label_codes', []) for q in questions))
        self.assertTrue(any(q['purpose'] == 'verify_joint_history' for q in questions))
        self.assertEqual(self.refresh()['api_requests'],2)
        self.finish_other_labels()

    def test_empty_answers_stay_unknown_and_follow_only_known_event_scopes(self):
        def empty(url,**kwargs):
            self.calls.append((url,kwargs['params']))
            return Mock(status_code=200,headers={},json=Mock(return_value={'ui':kwargs['params'],'runs':[], 'dungeons': []}))
        with patch.object(self,'transport',side_effect=empty):
            result=self.refresh()
        self.assertLessEqual(len(self.calls),2)
        self.assertEqual(len({(p['characterId'], p['dungeonId']) for _,p in self.calls}),len(self.calls))
        self.assertNotIn('carrying',service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
        self.calls.clear()
        with patch.object(self,'transport',side_effect=empty):
            self.finish_other_labels()

    def test_guest_question_requires_exact_core_and_dungeon_scope(self):
        original=service.build_evidence(self.player)
        for change in ({'core_members':['outsider']*4},{'dungeon_id':999999},{'prior_run_ids':{}},
                       {'reason':''},{'expected_change':''},{'purpose':'fill_data'}):
            with self.subTest(change=change):
                evidence=copy.deepcopy(original)
                questions=evidence['metrics']['investigation']['questions']
                questions[:]=[dict(questions[0],**change)]
                evidence['metrics']['behavior_review']['questions']=[]
                with patch('botend.services.player_observation.build_evidence',return_value=evidence):
                    self.assertEqual(self.refresh()['api_requests'],0)
                self.assertEqual(self.calls,[])

    def test_cache_before_target_event_is_refreshed_even_inside_ttl(self):
        key='620e5a03673b8c031b2708439ff9da0bfef5286bfb438593fedad851c368939b'
        entry=copy.deepcopy(self.data['histories'][key]['16359'])
        cutoff='2026-10-19T18:00:00+00:00'
        entry['fetched_at']=cutoff
        entry['runs']=[r for r in entry['runs'] if datetime.fromisoformat(r['completed_at'])<datetime.fromisoformat(cutoff)]
        row=self.rows[key]
        row.payload_json['mythic_plus_scored_history']={'16359':entry}
        row.save(update_fields=['payload_json'])
        result=self.refresh()
        self.assertEqual(result['followup_api_requests'],2)
        self.assertIn((309612162,16359),{(p['characterId'],p['dungeonId']) for _,p in self.calls})
        self.assertEqual(result['investigation']['stop_reason'],'budget_completed')
        self.assertNotIn('carrying',service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
