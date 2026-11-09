import json

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.dashboard.dashboard import DashboardView
from botend.models import BlizzardForumCategory, BlizzardForumTopic, MonitorTask, WowArticle
from botend.tests import test_dashboard_database_tables as database_contract


class BlizzardForumDashboardTests(TestCase):
    # Reuse the registered Dashboard route's existing authentication fixtures.
    setUp = database_contract.DashboardDatabaseTableContractTests.setUp
    post_action = database_contract.DashboardDatabaseTableContractTests.post_action

    def article(self, version='retail', *, source='blizzard_tracker', linked=True, category=True):
        sequence = WowArticle.objects.count() + 1
        topic = None
        board = None
        if linked:
            if category:
                board = BlizzardForumCategory.objects.create(
                    forum_url='https://us.forums.blizzard.com/en/wow', external_id=1000 + sequence,
                    name=f'Real board {sequence}', slug=f'board-{sequence}',
                    url=f'https://us.forums.blizzard.com/en/wow/c/board-{sequence}/{1000 + sequence}',
                    game_version=version,
                )
            topic = BlizzardForumTopic.objects.create(
                forum_url='https://us.forums.blizzard.com/en/wow', external_id=2000 + sequence,
                category=board,
            )
        row = WowArticle.objects.create(
            source=source, category='原分类', blizzard_topic=topic,
            title='PTR Forever Retail cross-version original title', title_cn='跨板块原始中文标题',
            author='Original author', reply_count=7,
            url=f'https://us.forums.blizzard.com/en/wow/t/original/{2000 + sequence}',
            description='完整描述', content='Original complete body\n' * 100,
            content_cn='完整中文正文\n' * 100,
            content_blocks=json.dumps([{'type': 'html', 'html': '<p>Original complete HTML</p>'}]),
            content_blocks_cn=json.dumps([{'type': 'html', 'html': '<p>完整中文HTML</p>'}]),
        )
        return row, board

    def list_payload(self, **overrides):
        response = self.post_action({
            'action': 'get_table_data', 'table_name': 'WowArticle', 'page_size': 10, **overrides,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'success')
        return payload

    def detail(self, row):
        response = self.post_action({'action': 'get_wow_article_detail', 'id': row.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')
        return response.json()['data']

    def test_real_board_channels_in_both_registered_apis_preserve_original_fields(self):
        for version, label in [('retail', '正式服'), ('forever', '无限服'), ('test', '测试服')]:
            row, board = self.article(version)
            payload = self.list_payload(search=row.url, wow_source=row.source, wow_category=row.category)
            self.assertEqual(payload['total_count'], 1)
            for item in [payload['data'][0], self.detail(row)]:
                with self.subTest(version=version, detail='content_blocks_cn' in item):
                    self.assertEqual(item['game_version'], version)
                    self.assertEqual(item['game_version_label'], label)
                    self.assertEqual(item['forum_category'], {
                        'id': board.external_id, 'name': board.name, 'url': board.url,
                    })
                    for field in ['id', 'title', 'title_cn', 'source', 'category', 'author', 'url',
                                  'reply_count', 'description', 'content', 'content_cn',
                                  'content_blocks', 'content_blocks_cn']:
                        self.assertEqual(item[field], getattr(row, field))
                    self.assertFalse(any(key.startswith('blizzard_topic__') for key in item))
            for derived in ['game_version', 'game_version_label', 'forum_category']:
                self.assertNotIn(derived, payload['fields'])
                self.assertNotIn(derived, payload['field_types'])
            row.refresh_from_db()
            self.assertEqual(row.title, 'PTR Forever Retail cross-version original title')
            self.assertEqual(row.title_cn, '跨板块原始中文标题')

    def test_unknown_missing_and_non_bluepost_sources_do_not_infer_labels(self):
        rows = [self.article('unrecognised')[0], self.article('')[0],
                self.article(linked=False)[0], self.article(category=False)[0]]
        rows.extend(self.article('retail', source=source)[0]
                    for source in ['wowhead', 'exwind', 'blizzard_cn', 'nga', 'unknown'])
        listed = {item['id']: item for item in self.list_payload()['data']}
        for row in rows:
            for item in [listed[row.pk], self.detail(row)]:
                with self.subTest(source=row.source, row=row.pk):
                    self.assertEqual(item['game_version'], '')
                    self.assertEqual(item['game_version_label'], '')
                    if row.source != 'blizzard_tracker' or not row.blizzard_topic_id:
                        self.assertIsNone(item['forum_category'])

    def test_pagination_and_filters_keep_fixed_query_count_and_scalar_joins(self):
        row, _ = self.article()
        view = DashboardView()
        params = {'table_name': 'WowArticle', 'page_size': 10, 'wow_source': 'blizzard_tracker',
                  'wow_category': '原分类'}
        with self.assertNumQueries(4):
            small = json.loads(view.get_table_data(params).content)
        self.assertEqual(small['status'], 'success')
        for _ in range(24):
            self.article('forever')
        self.article('test', source='wowhead')
        with CaptureQueriesContext(connection) as captured:
            payload = self.list_payload(page=2, wow_source='blizzard_tracker', wow_category='原分类')
        article_queries = [q['sql'] for q in captured if 'FROM "botend_wowarticle"' in q['sql']]
        self.assertEqual(len(article_queries), 4)
        page_sql = next(sql for sql in article_queries if ' LIMIT ' in sql)
        self.assertIn('LIMIT 10 OFFSET 10', page_sql)
        self.assertEqual(page_sql.count('LEFT OUTER JOIN'), 2)
        self.assertNotIn('response_sha256', page_sql)
        self.assertNotIn('source_url', page_sql)
        self.assertEqual((payload['total_count'], payload['total_pages'], payload['page']), (25, 3, 2))
        expected_ids = list(WowArticle.objects.filter(source='blizzard_tracker').order_by('-pk')
                            .values_list('pk', flat=True)[10:20])
        self.assertEqual([item['id'] for item in payload['data']], expected_ids)
        self.assertTrue(all(item['game_version_label'] == '无限服' for item in payload['data']))
        with self.assertNumQueries(1):
            detail = json.loads(view.get_wow_article_detail({'id': row.pk}).content)
        self.assertEqual(detail['data']['content_cn'], row.content_cn)

    def test_other_tables_keep_original_schema_and_permissions_still_apply(self):
        task = MonitorTask.objects.create(name='Original monitor', target='https://example.invalid')
        payload = self.post_action({'action': 'get_table_data', 'table_name': 'MonitorTask'}).json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['data'][0]['id'], task.pk)
        self.assertNotIn('game_version', payload['data'][0])
        self.assertEqual(set(payload['data'][0]), set(payload['fields']))
        row, _ = self.article()
        response = self.post_action(
            {'action': 'get_table_data', 'table_name': 'WowArticle'}, user=self.user)
        self.assertEqual(response.status_code, 403)
        # Existing detail action permits authenticated readers; do not alter that policy here.
        response = self.post_action({'action': 'get_wow_article_detail', 'id': row.pk}, user=self.user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['id'], row.pk)
        self.client.logout()
        response = self.client.post('/dashboard/',
                                    data=json.dumps({'action': 'get_wow_article_detail', 'id': row.pk}),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 302)
