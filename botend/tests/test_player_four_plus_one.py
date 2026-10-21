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
    def test_anuo_two_distinct_beneficiaries_without_all_dungeon_protection(self):
        args=anuo_args(); result=build_behavior_review(**args)
        self.assertIn('carrying', result['supported'])
        self.assertGreaterEqual(len(result['witnesses']['carrying']),2)
        self.assertEqual(result['questions'],[])
        grade = result['evidence_strengths']['carrying']
        self.assertEqual((grade['level'], grade['confidence']), ('strong', '高'))
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
        self.assertIn('carrying',result['supported'])

    def test_guest_untimed_higher_score_also_counters_an_increase(self):
        args=anuo_args()
        key='620e5a03673b8c031b2708439ff9da0bfef5286bfb438593fedad851c368939b'
        for run in args['histories'][key]['16359']['runs']:
            if run['run_id']==21627600:
                run.update(level=27,score=540,timed=False)
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertFalse(any(q['member_key']==key for q in result['questions']))

    def test_fixed_five_and_single_guest_multiple_runs_are_not_rotation(self):
        args=anuo_args()
        ids={21631117,21646965,21654678,21663568}
        args['recent']=[r for r in args['recent'] if int(r['url'].split('/')[5]) in ids]
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertEqual(result['questions'],[])
        self.assertEqual(result['evidence_strengths']['carrying']['level'], 'insufficient')

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
        self.assertEqual(result['questions'],[])
        self.assertEqual(result['evidence_strengths']['carrying']['level'], 'insufficient')

    def test_slot_order_does_not_define_the_rotating_member(self):
        args=anuo_args()
        for raw, members in args['rosters'].values():
            members.reverse()
        self.assertIn('carrying',build_behavior_review(**args)['supported'])

    def test_two_isolated_runs_are_insufficient_for_repeated_collaboration(self):
        args=anuo_args()
        args['recent']=[r for r in args['recent'] if int(r['url'].split('/')[5]) in {21551218,21654678}]
        result=build_behavior_review(**args)
        self.assertNotIn('carrying',result['supported'])
        self.assertEqual(result['questions'],[])

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
        for q in result['questions']:
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
        self.assertTrue(url.endswith('/mythic-plus-runs'), (url,kwargs['params']))
        params = kwargs['params']
        history = next(h[str(params['dungeonId'])] for h in self.data['histories'].values()
            if str(params['dungeonId']) in h and h[str(params['dungeonId'])]['ui']['characterId'] == params['characterId'])
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

    def test_anuo_real_cache_two_questions_then_zero_requests_and_publishable_tag(self):
        before = service.build_evidence(self.player)
        self.assertNotIn('carrying',before['metrics']['behavior_review']['supported'])
        result = self.refresh()
        self.assertEqual(result['basic_api_requests'],0)
        self.assertEqual(result['followup_api_requests'],2)
        self.assertEqual(result['investigation']['stop_reason'],'completed')
        self.assertEqual(result['investigation']['remaining_question_ids'],[])
        self.assertEqual({(p['characterId'],p['dungeonId']) for _,p in self.calls},
                         {(305720261,9527),(309612162,16359)})
        for trace in result['investigation']['requests']:
            self.assertEqual(trace['question']['purpose'],'verify_guest_gain')
            self.assertTrue(trace['question']['reason']);self.assertTrue(trace['question']['expected_change'])
        after = service.build_evidence(self.player)
        self.assertIn('carrying',after['metrics']['behavior_review']['supported'])
        self.assertEqual(after['metrics']['behavior_review']['questions'],[])
        self.calls.clear()
        self.assertEqual(self.refresh()['api_requests'],0)
        self.assertEqual(self.calls,[])
        from botend.tests.test_player_observation import structured_response
        def choose(prompt, **kwargs):
            data=json.loads(prompt)
            label=next(x for x in data['eligible_labels'] if x['code']=='carrying')
            return json.dumps({'observations':[dict(code='carrying',confidence=label['confidence_max'],
                reason_id='repeated_witnesses',limitation_ids=label['required_limitation_ids'],
                evidence_ids=label['evidence_ids'])], 'conclusion_id':'public_sample',
                'investigation':structured_response(data['metrics']['investigation']['response_contract'])},ensure_ascii=False)
        engine=Mock(send_message=Mock(side_effect=choose))
        self.assertEqual(service.generate_observation(self.player,engine=engine)['status'],'ready')
        view=service.project_player_observation(self.player)
        self.assertTrue(any('4带1' in x['label'] for x in view['observations']))
        judgement = next(j for j in view['display_judgements'] if j['code'] == 'carrying')
        self.assertEqual(judgement['value'], '4带1协助上分模式 · 证据充分')
        self.assertEqual(judgement['evidence_strength']['confidence'], '高')
        from django.template.loader import render_to_string
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': view})
        self.assertIn('未核实信息：', html)
        self.assertIn('是否收费或存在交易', html)
        self.assertNotIn('低置信', html)
        # 模型不能把未知动机转移成对行为证据的统一降级。
        invalid = json.loads(choose(json.dumps(service._generation_input(after))))
        invalid['observations'][0]['confidence'] = '低'
        with self.assertRaisesRegex(service.InvalidResult, 'invalid_evidence_strength'):
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

    def test_missing_rosters_prefer_higher_prior_attainment_then_stop(self):
        # 复现线上起点：小花生的受益已缓存，两场黑按摩捣师阵容缺失。
        # 最新场次21663568只有同层跑前成绩；21654678有24层跑前证明。
        guest = '52affcc3c4c1a22d15de2f13abcdf59b2eb5d369272475c0a9919f090bacbefc'
        row = self.rows[guest]
        row.payload_json['mythic_plus_scored_history'] = self.data['histories'][guest]
        row.save(update_fields=['payload_json'])
        for rid in (21663568, 21654678):
            RaiderIORunRoster.objects.filter(run_id=rid).update(payload_json={}, status='pending', fetched_at=None)
        self.assertNotIn('carrying', service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
        result = self.refresh()
        self.assertEqual([(url.rsplit('/', 1)[-1], params.get('id'), params.get('dungeonId'))
                          for url, params in self.calls],
                         [('run-details', 21654678, None), ('mythic-plus-runs', None, 16359)])
        self.assertEqual(result['api_requests'], 2)
        self.assertEqual(result['basic_api_requests'], 1)
        self.assertEqual(result['followup_api_requests'], 1)
        request = next(r for r in result['runs'] if r['status'] == 'ready')
        self.assertEqual(request['question']['prior_level_advantage'], 1)
        self.assertTrue(request['question']['selection_reason'])
        self.assertIn('carrying', service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
        self.assertEqual(result['investigation']['stop_reason'], 'completed')
        self.assertEqual(result['investigation']['remaining_question_ids'], [])
        self.assertEqual(RaiderIORunRoster.objects.get(run_id=21663568).status, 'pending')
        self.calls.clear()
        self.assertEqual(self.refresh()['api_requests'], 0)
        self.assertEqual(self.calls, [])

    def test_normal_substitutes_do_not_trigger_requests_or_carrying(self):
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
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(self.calls, [])
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

    def test_fixed_five_uses_no_history_requests(self):
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
        self.assertEqual(evidence['metrics']['behavior_review']['questions'],[])
        self.assertEqual(self.refresh()['api_requests'],0)

    def test_empty_answer_stays_unknown_and_does_not_expand_to_other_dungeons(self):
        def empty(url,**kwargs):
            self.calls.append((url,kwargs['params']))
            return Mock(status_code=200,headers={},json=Mock(return_value={'ui':kwargs['params'],'runs':[]}))
        with patch.object(self,'transport',side_effect=empty):
            result=self.refresh()
        self.assertLessEqual(len(self.calls),2)
        self.assertEqual(len({p['characterId'] for _,p in self.calls}),len(self.calls))
        self.assertNotIn('carrying',service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
        self.calls.clear()
        with patch.object(self,'transport',side_effect=empty):
            self.assertEqual(self.refresh()['api_requests'],0)

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
        self.assertEqual(result['investigation']['stop_reason'],'completed')
        self.assertIn('carrying',service.build_evidence(self.player)['metrics']['behavior_review']['supported'])
