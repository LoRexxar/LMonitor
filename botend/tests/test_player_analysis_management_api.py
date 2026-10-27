"""HTTP/ORM contracts for the independent superuser analysis workbench.

No service or ORM mocks: these tests exercise the actual management service.
"""
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from botend.models import (
    DashboardUserGroup, PlayerObservationAnalysis, PlayerSpecTopPlayer, SeasonMeta,
)


PAGE = '/dashboard/player-analyses/'
COLLECTION = '/dashboard/api/player-analyses/'
CHARACTERS = COLLECTION + 'characters/'


class PlayerAnalysisManagementAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.admin = users.create_user('analysis-root', is_superuser=True, is_staff=True)
        cls.staff = users.create_user('analysis-staff', is_staff=True)
        cls.member = users.create_user('analysis-member')
        group = DashboardUserGroup.objects.create(
            name='analysis-admin-menu', permission_codes=['system.ai-management', 'database.manage'])
        group.users.add(cls.staff, cls.member)
        cls.season = SeasonMeta.objects.create(
            season_key='analysis-api-season', season_name='API season',
            mplus_zone_id=123, raid_zone_id=456)
        cls.player = cls.make_player('AnalysisAlpha')

    @classmethod
    def make_player(cls, name):
        return PlayerSpecTopPlayer.objects.create(
            season_id=cls.season.pk, region='eu', realm='silvermoon',
            character_name=name, class_name='Mage', spec_name='frost', rank=1,
            profile_url=f'https://raider.io/characters/eu/silvermoon/{name}')

    def setUp(self):
        self.client.force_login(self.admin)

    def payload(self, player=None):
        return {
            'player_id': (player or self.player).pk,
            'manual': {
                'tags': [{'label': '观察', 'value': '待核实', 'reason': '人工记录',
                          'evidence': [{'text': '公开资料', 'url': 'https://example.com/evidence'}]}],
                'conclusion': '仅记录可核验证据',
                'evidence': [{'text': '来源', 'url': 'https://example.com/source'}],
            },
            'review_status': 'pending', 'is_published': False, 'review_note': '',
        }

    def send(self, method, path, payload, client=None, **extra):
        return getattr(client or self.client, method)(
            path, data=json.dumps(payload), content_type='application/json', **extra)

    def success(self, response, allowed=(200,)):
        self.assertIn(response.status_code, allowed, response.content.decode())
        body = response.json()
        self.assertEqual(body['status'], 'success')
        return body['data']

    def create(self, player=None, client=None, **extra):
        return self.success(self.send('post', COLLECTION, self.payload(player), client, **extra), (200, 201))

    def detail_path(self, analysis):
        return COLLECTION + str(analysis['id']) + '/'

    def error(self, response, status):
        self.assertEqual(response.status_code, status, response.content.decode())
        body = response.json()
        self.assertEqual(body['status'], 'error')
        self.assertIsInstance(body['message'], str)
        return body

    def test_page_list_detail_characters_and_all_writes_are_superuser_only(self):
        analysis = self.create()
        path = self.detail_path(analysis)
        before = list(PlayerObservationAnalysis.objects.values())
        for user in (None, self.member, self.staff):
            if user is None:
                self.client.logout()
            else:
                self.client.force_login(user)
            with self.subTest(user=user):
                self.assertEqual(self.client.get(PAGE).status_code, 403)
                for url in (COLLECTION, CHARACTERS, path):
                    self.error(self.client.get(url), 403)
                self.error(self.send('post', COLLECTION, self.payload()), 403)
                self.error(self.send('patch', path, {'revision': analysis['revision'], 'manual': None}), 403)
                self.error(self.send('delete', path, {'revision': analysis['revision']}), 403)
        self.assertEqual(list(PlayerObservationAnalysis.objects.values()), before)

    def test_list_uses_frozen_label_without_conclusion_or_detail_payload(self):
        row = self.create()
        PlayerObservationAnalysis.objects.filter(pk=row['id']).update(manual_json={},
            result_json={'observations': [{'code': 'fixed_team', 'confidence': '低'}],
                         'conclusion_id': 'public_sample'},
            evidence_json={'eligible_labels': [{'code': 'fixed_team', 'label': '固定队'}],
                'conclusion_options': {'public_sample': {'text': '冻结的角色结论'}},
                'large_fact': 'x' * 100000})
        with self.assertNumQueries(5):
            response = self.client.get(COLLECTION, {'q': row['id']})
        with self.assertNumQueries(4):
            self.client.get(COLLECTION, {'q': row['id'], 'include_tag_options': '0'})
        item = self.success(response)['items'][0]
        self.assertEqual(item['tags'], [{'label': '固定队', 'value': '低'}])
        self.assertEqual(self.success(response)['tag_options'], ['固定队'])
        self.assertNotIn('conclusion', item)
        self.assertEqual((item['player']['class_label'], item['player']['spec_label']), ('法师', '冰霜'))
        self.assertEqual(item['player']['season_name'], self.season.season_name)
        self.assertNotIn('evidence_json', item)
        self.assertNotContains(response, 'large_fact')
        filtered = self.success(self.client.get(COLLECTION, {'tag': '固定队', 'page_size': '1'}))
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(filtered['items'][0]['id'], row['id'])
        unknown = self.success(self.client.get(COLLECTION, {'tag': '未知'}))
        self.assertEqual(unknown['total'], 0)
        self.assertEqual(unknown['tag_options'], ['固定队'])
        self.error(self.client.get(COLLECTION, {'tag': 'x' * 201}), 400)

    def test_real_dashboard_section_and_followed_legacy_entry(self):
        target = '/dashboard/?section=player-analyses'
        for response in (self.client.get(target), self.client.get(PAGE, follow=True)):
            self.assertEqual(response.status_code, 200, response.content[:500])
            self.assertContains(response, 'id="player-analyses"', count=1)
            self.assertContains(response, 'id="player-analysis-app"', count=1)
            self.assertContains(response, 'data-section="player-analyses"', count=1)
            self.assertIn('no-store', response.get('Cache-Control', ''))
        followed = self.client.get(PAGE, follow=True)
        self.assertEqual(followed.redirect_chain, [(target, 302)])

    def test_direct_section_cannot_be_granted_to_non_superusers(self):
        group = DashboardUserGroup.objects.create(name='analysis-forged-menu',
            permission_codes=['dashboard.home', 'system.player-analyses'])
        group.users.add(self.staff, self.member)
        for user in (self.staff, self.member):
            self.client.force_login(user)
            self.assertEqual(self.client.get('/dashboard/?section=player-analyses').status_code, 403)
            self.assertEqual(self.client.get(PAGE, follow=True).status_code, 403)
            self.assertEqual(self.client.get(COLLECTION).status_code, 403)

    def test_csrf_blocks_every_write_and_valid_token_allows_crud(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        analysis = self.create()
        path = self.detail_path(analysis)
        before = list(PlayerObservationAnalysis.objects.values())
        for method, url, data in (
            ('post', COLLECTION, self.payload()),
            ('patch', path, {'revision': analysis['revision'], 'review_note': 'blocked'}),
            ('delete', path, {'revision': analysis['revision']}),
        ):
            self.assertEqual(self.send(method, url, data, client).status_code, 403)
        self.assertEqual(list(PlayerObservationAnalysis.objects.values()), before)
        page = client.get(PAGE)
        self.assertRedirects(page, '/dashboard/?section=player-analyses', fetch_redirect_response=False)
        self.assertIn('no-store', page['Cache-Control'])
        token = client.cookies['csrftoken'].value
        headers = {'HTTP_X_CSRFTOKEN': token}
        created = self.create(self.make_player('CSRFValid'), client=client, **headers)
        created_path = self.detail_path(created)
        updated = self.success(self.send('patch', created_path, {
            'revision': created['revision'], 'review_note': '合法 CSRF'}, client, **headers))
        self.success(self.send('delete', created_path, {'revision': updated['revision']}, client, **headers))
        self.assertTrue(PlayerObservationAnalysis.objects.filter(pk=created['id']).exists())

    def test_create_update_clear_override_and_archive_preserve_ai(self):
        created = self.create()
        self.assertTrue({'id', 'revision', 'player_id', 'player', 'ai', 'manual',
                         'effective', 'review_status', 'is_published'} <= created.keys())
        path = self.detail_path(created)
        row = PlayerObservationAnalysis.objects.get(pk=created['id'])
        ai_before = (row.result_json, row.evidence_json, row.evidence_hash)
        self.assertEqual(created['player_id'], self.player.pk)
        self.assertEqual(created['manual']['conclusion'], self.payload()['manual']['conclusion'])
        modified = self.success(self.send('patch', path, {
            'revision': created['revision'], 'review_note': '已复核',
            'review_status': 'approved', 'is_published': True}))
        self.assertGreater(modified['revision'], created['revision'])
        self.assertTrue(modified['is_published'])
        self.assertEqual(self.success(self.client.get(path))['revision'], modified['revision'])
        self.success(self.send('delete', path, {'revision': modified['revision']}))
        row.refresh_from_db()
        self.assertEqual((row.result_json, row.evidence_json, row.evidence_hash), ai_before)
        self.assertTrue(PlayerObservationAnalysis.objects.filter(pk=row.pk).exists())
        archived = self.success(self.client.get(path))
        self.assertFalse(archived['is_published'])
        self.assertGreater(archived['revision'], modified['revision'])
        self.assertEqual(archived['manual'], modified['manual'])
        cleared = self.success(self.send('patch', path, {'revision': archived['revision'], 'manual': None}))
        self.assertIsNone(cleared['manual'])
        row.refresh_from_db()
        self.assertEqual((row.result_json, row.evidence_json, row.evidence_hash), ai_before)

    def test_stale_revision_and_duplicate_create_are_conflicts_without_mutation(self):
        analysis = self.create()
        path = self.detail_path(analysis)
        self.error(self.send('post', COLLECTION, self.payload()), 409)
        updated = self.success(self.send('patch', path, {
            'revision': analysis['revision'], 'review_note': 'first edit'}))
        before = list(PlayerObservationAnalysis.objects.values())
        for method in ('patch', 'delete'):
            self.error(self.send(method, path, {'revision': analysis['revision']}), 409)
            self.error(self.send(method, path, {}), 400)
        self.assertEqual(list(PlayerObservationAnalysis.objects.values()), before)
        self.assertEqual(self.success(self.client.get(path))['revision'], updated['revision'])

    def test_reads_are_side_effect_free_and_search_and_pagination_use_real_rows(self):
        first = self.create()
        self.create(self.make_player('AnalysisBeta'))
        before = list(PlayerObservationAnalysis.objects.values())
        page = self.client.get(PAGE)
        self.assertRedirects(page, '/dashboard/?section=player-analyses', fetch_redirect_response=False)
        self.assertIn('no-store', page['Cache-Control'])
        self.success(self.client.get(self.detail_path(first)))
        for url in (COLLECTION, CHARACTERS):
            data = self.success(self.client.get(url, {'q': 'Analysis', 'page_size': 1, 'page': 1}))
            self.assertEqual((data['total'], data['page'], data['page_size'], data['total_pages']), (2, 1, 1, 2))
            second = self.success(self.client.get(url, {'q': 'Analysis', 'page_size': 1, 'page': 2}))
            self.assertEqual(len(data['items']), 1)
            self.assertEqual(len(second['items']), 1)
            self.assertNotEqual(data['items'], second['items'])
            filtered = self.success(self.client.get(url, {'q': 'AnalysisAlpha'}))
            self.assertEqual(filtered['total'], 1)
            self.assertEqual(self.success(self.client.get(url, {'q': 'NoSuchCharacter'}))['total'], 0)
        self.assertEqual(list(PlayerObservationAnalysis.objects.values()), before)

    def test_unknown_ids_fields_and_invalid_pagination_are_rejected(self):
        analysis = self.create()
        path = self.detail_path(analysis)
        before = list(PlayerObservationAnalysis.objects.values())
        for method in ('get', 'patch', 'delete'):
            if method == 'get':
                response = self.client.get(COLLECTION + '99999999/')
            else:
                response = self.send(method, COLLECTION + '99999999/', {'revision': 1})
            self.error(response, 404)
        for field in ('unknown', 'result_json', 'activity_id', 'lease_token', 'id'):
            self.error(self.send('patch', path, {'revision': analysis['revision'], field: 'forbidden'}), 400)
        self.error(self.send('delete', path, {'revision': analysis['revision'], 'unknown': True}), 400)
        payload = self.payload(self.make_player('InvalidCreate'))
        payload['unknown'] = True
        self.error(self.send('post', COLLECTION, payload), 400)
        for query in ({'page': 'bad'}, {'page_size': 'bad'}, {'review_status': 'invalid'},
                      {'source': 'invalid'}, {'visibility': 'invalid'}, {'status': 'invalid'}):
            self.error(self.client.get(COLLECTION, query), 400)
        self.assertEqual(list(PlayerObservationAnalysis.objects.values()), before)

    def test_json_objects_size_limit_and_methods(self):
        analysis = self.create()
        path = self.detail_path(analysis)
        before = list(PlayerObservationAnalysis.objects.values())
        for raw in ('[]', 'null', '42', '"text"', '{', '{"revision":NaN}', '{"revision":1,"revision":2}'):
            for method, url in (('post', COLLECTION), ('patch', path), ('delete', path)):
                self.error(getattr(self.client, method)(url, raw, content_type='application/json'), 400)
        self.error(self.client.post(COLLECTION, b'\xff', content_type='application/json'), 400)
        self.error(self.send('post', COLLECTION, {'padding': 'x' * (128 * 1024)}), 413)
        for method, url, allowed in (
            ('put', COLLECTION, 'GET, POST'), ('post', path, 'GET, PATCH, DELETE'),
            ('post', CHARACTERS, 'GET'), ('delete', PAGE, 'GET'),
        ):
            response = self.send(method, url, {})
            self.error(response, 405)
            self.assertEqual(response['Allow'], allowed)
        self.assertEqual(list(PlayerObservationAnalysis.objects.values()), before)

    def test_xss_text_is_preserved_as_json_data_and_unsafe_urls_are_rejected(self):
        text = '<script>alert("测试")</script> & "quoted" <img src=x onerror=alert(1)>'
        payload = self.payload()
        payload['manual']['conclusion'] = text
        payload['manual']['tags'][0]['reason'] = text
        payload['manual']['evidence'][0]['text'] = text
        created = self.success(self.send('post', COLLECTION, payload), (200, 201))
        response = self.client.get(self.detail_path(created))
        self.assertEqual(response['Content-Type'], 'application/json')
        detail = self.success(response)
        self.assertEqual(detail['manual']['conclusion'], text)
        self.assertEqual(detail['manual']['tags'][0]['reason'], text)
        self.assertEqual(detail['manual']['evidence'][0]['text'], text)
        before = list(PlayerObservationAnalysis.objects.values())
        for url in ('javascript:alert(1)', 'data:text/html,<script>alert(1)</script>'):
            manual = self.payload()['manual']
            manual['evidence'][0]['url'] = url
            self.error(self.send('patch', self.detail_path(created), {
                'revision': created['revision'], 'manual': manual}), 400)
        self.assertEqual(list(PlayerObservationAnalysis.objects.values()), before)
