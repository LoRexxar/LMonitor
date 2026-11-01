"""TEST SYNTHESIS ONLY: six independent assistance events, never live evidence.

All identities, IDs, dates and score changes below are invented. HTTP responses
are injected into the real collectors; no parser, ORM, builder, policy or result
validator is replaced. The deterministic model boundary never calls paid AI.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from botend.models import SeasonMeta, PlayerSpecTopPlayer, RaiderIOCharacterActivity
from botend.services.player_observation_history import scope as history_scope
from botend.services.player_observation_sources import _scope, refresh_observation_sources

NOW = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
SEASON = 'season-mn-2'
DUNGEON = {'id': 1, 'name': 'Synthetic Test Dungeon', 'slug': 'synthetic-test-dungeon'}


def synthetic_sources(count=6, *, one_guest=False, duplicate=False):
    """Protocol-complete raw API payloads, not precomputed behavior evidence."""
    characters = []
    for index in range(6):
        name = f'SyntheticCarrying{index + 1}'
        role = 'tank' if index == 0 else 'dps' if index < 4 else 'healer'
        class_id, class_name, class_slug, spec_id, spec_name, spec_slug = (
            (6, 'Death Knight', 'death-knight', 250, 'Blood', 'blood') if role == 'tank' else
            (1, 'Warrior', 'warrior', 71, 'Arms', 'arms') if role == 'dps' else
            (2, 'Paladin', 'paladin', 65, 'Holy', 'holy'))
        characters.append(dict(id=900001 + index, name=name,
            path=f'/characters/cn/synthetic-realm/{name}',
            region={'slug': 'cn'}, realm={'slug': 'synthetic-realm'},
            **{'class': {'id': class_id, 'name': class_name, 'slug': class_slug}},
            spec={'id': spec_id, 'name': spec_name, 'slug': spec_slug, 'role': role}))
    recent, rosters, histories, scores = [], {}, {}, {}
    for index, character in enumerate(characters):
        cid = character['id']
        prior = dict(season=SEASON, dungeon=copy.deepcopy(DUNGEON),
            keystone_run_id=910001 + index, mythic_level=25 if index < 4 else 15,
            score=600 if index < 4 else 400, num_chests=1,
            completed_at=(NOW - timedelta(days=2)).isoformat(),
            role=character['spec']['role'], alternate=None)
        scores[cid] = dict(ui=_scope(SEASON, cid), dungeons=[prior])
        histories[cid] = dict(ui=history_scope(SEASON, cid, 1), runs=[
            dict(score=prior['score'], summary={key: copy.deepcopy(value)
                for key, value in prior.items() if key not in ('score', 'role', 'alternate')})])
    for index in range(count):
        rid = 920001 + index
        stamp = (NOW - timedelta(hours=3) + timedelta(minutes=10 * index)).isoformat()
        guest = characters[4 if one_guest or index % 2 == 0 else 5]
        recent.append(dict(url=f'https://raider.io/mythic-plus-runs/{SEASON}/{rid}',
            dungeon=DUNGEON['name'], zone_id=1, mythic_level=20,
            score=500 + index, completed_at=stamp, clear_time_ms=300000,
            num_keystone_upgrades=1))
        rosters[str(rid)] = dict(season=SEASON, keystone_run_id=rid,
            dungeon=copy.deepcopy(DUNGEON), mythic_level=20, clear_time_ms=300000,
            num_chests=1, completed_at=stamp, runPrivacyMode='public', deleted_at=None,
            roster=[dict(role=c['spec']['role'], character=copy.deepcopy(c))
                    for c in characters[:4] + [guest]])
        summary = dict(season=SEASON, dungeon=copy.deepcopy(DUNGEON),
            keystone_run_id=rid, mythic_level=20, num_chests=1, completed_at=stamp)
        histories[guest['id']]['runs'].append(dict(score=500 + index, summary=summary))
        scores[guest['id']]['dungeons'] = [dict(**summary, score=500 + index,
            role=guest['spec']['role'], alternate=None)]
    recent.reverse()
    if duplicate:
        # Still bounded by the real profile API's 10-recent-run protocol.
        recent += copy.deepcopy(recent[:max(0, 10 - len(recent))])
        for history in histories.values():
            history['runs'] *= 2
    subject = characters[0]
    profile = dict(region='cn', realm='synthetic-realm', name=subject['name'],
        profile_url='https://raider.io' + subject['path'], last_crawled_at=NOW.isoformat(),
        mythic_plus_scores_by_season=[{'season': SEASON}], mythic_plus_best_runs=[],
        mythic_plus_recent_runs=recent, mythic_plus_dungeon_run_counts=[
            dict(dungeon=DUNGEON['name'], zone_id=1, season_runs_total=count, season_runs_timed=count)],
        raid_progression={})
    return dict(profile=profile, rosters=rosters, scores=scores, histories=histories,
                characters=characters, synthesis='TEST ONLY; no real player or online evidence')


class CarryingPipelineFixture:
    """Reusable methods for Django TestCase consumers (not itself a test class)."""
    def setUp(self, count=6, *, one_guest=False, duplicate=False):
        self.now = NOW
        clock = patch('django.utils.timezone.now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.data = synthetic_sources(count, one_guest=one_guest, duplicate=duplicate)
        self.calls = []
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='合成测试赛季',
            rio_season=SEASON, mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            region='cn', realm='synthetic-realm', character_name=self.data['profile']['name'],
            class_name='DeathKnight', spec_name='Blood', rank=1,
            profile_url=self.data['profile']['profile_url'])
        self.activity = RaiderIOCharacterActivity.objects.create(season=self.season,
            region='cn', realm_slug=self.player.realm, character_name=self.player.character_name)
        self._sources_collected = False

    def transport(self, url, **kwargs):
        params = copy.deepcopy(kwargs['params'])
        self.calls.append((url, params))
        if url.endswith('/characters/profile'):
            self.assertEqual((params['region'], params['realm'], params['name']),
                ('cn', 'synthetic-realm', self.player.character_name))
            payload = self.data['profile']
        elif url.endswith('/run-details'):
            payload = self.data['rosters'][str(params['id'])]
        elif url.endswith('/mythic-plus-scored-runs'):
            self.assertEqual(params, _scope(SEASON, params['characterId']))
            payload = self.data['scores'][params['characterId']]
        else:
            self.assertTrue(url.endswith('/mythic-plus-runs'), (url, params))
            self.assertEqual(params, history_scope(SEASON, params['characterId'], params['dungeonId']))
            payload = self.data['histories'][params['characterId']]
            if params['dungeonId'] != 1:
                payload = dict(ui=params, runs=[])
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        response.encoding = 'utf-8'
        return response

    def refresh(self):
        # Only network and wall-clock pacing are injected. All collection, scope
        # validation, cache publication, re-planning and reads are production code.
        with patch('botend.services.player_observation_sources.requests.Session.get', side_effect=self.transport), \
                patch('botend.services.player_observation_sources._pace', return_value=True), \
                patch('botend.services.raiderio_run_roster._pace', return_value=True):
            if not self._sources_collected:
                initial = refresh_observation_sources(self.player, force=True)
                self.assertEqual(initial['status'], 'ready', initial)
                self._sources_collected = True
            result = refresh_observation_sources(self.player, progressive=True, force=True)
            self.assertLessEqual(result['followup_api_requests'], 2)
        self.activity.refresh_from_db()
        return result


def synthetic_model_response(prompt, **kwargs):
    """Deterministic TEST ONLY model output, still subjected to real validation."""
    from botend.tests.test_player_observation import structured_response
    data = json.loads(prompt)
    carrying = next((item for item in data['eligible_labels'] if item['code'] == 'carrying'), None)
    labels = [] if carrying is None else [dict(code='carrying', confidence='高',
        reason_id='repeated_witnesses', limitation_ids=carrying['required_limitation_ids'],
        evidence_ids=carrying['evidence_ids'])]
    return json.dumps(dict(observations=labels, conclusion_id='public_sample',
        investigation=structured_response(data['metrics']['investigation']['response_contract'])),
        ensure_ascii=False)
