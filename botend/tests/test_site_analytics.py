import json
import uuid
from datetime import datetime
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import HttpResponse, StreamingHttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from botend.analytics.middleware import SiteAnalyticsMiddleware
from botend.analytics.models import SiteAnalyticsConfig, SitePageView
from botend.analytics.views import client_ip, digest
from botend.models import DashboardUserGroup, DashboardUserGroupMembership


COLLECT = '/api/site-analytics/collect/'
API = '/api/dashboard/site-analytics/'


class InjectionTests(SimpleTestCase):
    def apply(self, response, method='get'):
        return SiteAnalyticsMiddleware(lambda request: response)(getattr(RequestFactory(), method)('/'))

    def test_inject_once_and_fix_headers(self):
        response = HttpResponse('<HTML><BODY>中文</BODY></HTML>')
        response['Content-Length'] = len(response.content)
        response['ETag'] = '旧摘要'
        self.apply(response)
        self.apply(response)
        self.assertEqual(response.content.count(b'data-site-analytics'), 1)
        self.assertIn(b'</script></BODY>', response.content)
        self.assertEqual(int(response['Content-Length']), len(response.content))
        self.assertNotIn('ETag', response)

    def test_html_without_body_close(self):
        self.assertIn(b'data-site-analytics', self.apply(HttpResponse('<html><p>报告')).content)

    def test_non_pages_and_sandbox_unchanged(self):
        for response in [HttpResponse('{}', content_type='application/json'),
                         HttpResponse('<p>片段</p>'), HttpResponse('<html>错误', status=500),
                         HttpResponse('<html>下载', headers={'Content-Disposition': 'attachment'}),
                         HttpResponse('<html>沙箱', headers={'Content-Security-Policy': 'sandbox allow-scripts'}),
                         HttpResponse(b'compressed', headers={'Content-Encoding': 'gzip'})]:
            before = response.content
            self.assertEqual(self.apply(response).content, before)
        response = StreamingHttpResponse(iter([b'<html>']))
        self.assertIs(self.apply(response), response)

    def test_head_does_not_change_response(self):
        self.assertNotIn(b'data-site-analytics', self.apply(HttpResponse('<html>'), 'head').content)

    def test_standalone_report_route_is_injected(self):
        report = Mock()
        report.name = '统计测试报告.html'
        report.read_text.return_value = '<html><body>独立报告</body></html>'
        with patch('botend.portal.views._resolve_portal_report_html_path', return_value=report):
            response = self.client.get('/portal/reports/analytics-test.html')
        self.assertContains(response, '独立报告')
        self.assertContains(response, 'data-site-analytics', count=1)


class CollectionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(enforce_csrf_checks=True)
        self.payload = {'event_id': str(uuid.uuid4()), 'visitor_id': str(uuid.uuid4()),
                        'session_id': str(uuid.uuid4()), 'path': '/portal/news/?secret=abc#token',
                        'referrer': 'https://example.org/read?token=secret'}

    def send(self, data=None, **headers):
        return self.client.post(COLLECT, json.dumps(self.payload if data is None else data), content_type='application/json',
                                **{'HTTP_ORIGIN': 'http://testserver', 'REMOTE_ADDR': '192.0.2.8',
                                   'HTTP_USER_AGENT': 'Mozilla/5.0 Windows Chrome/120', **headers})

    def test_collect_minimal_dimensions_and_deduplicate(self):
        self.assertEqual(self.send().status_code, 204)
        self.assertEqual(self.send().status_code, 204)
        self.assertEqual(SitePageView.objects.count(), 1)
        event = SitePageView.objects.get()
        self.assertEqual(event.path, '/portal/news/')
        self.assertEqual(event.referrer_host, 'example.org')
        self.assertEqual(event.ip_key, digest('ip', '192.0.2.8'))
        self.assertNotEqual(event.visitor_key, self.payload['visitor_id'])
        self.assertEqual((event.area, event.device, event.browser, event.os), ('前台', '电脑', 'Chrome', 'Windows'))
        self.assertFalse(event.authenticated)

    def test_origin_required_and_foreign_rejected(self):
        for value in ['', 'null', 'http://evil.example', 'http://testserver.evil', 'https://testserver']:
            self.assertEqual(self.send(HTTP_ORIGIN=value).status_code, 403)
        self.assertEqual(self.send(HTTP_SEC_FETCH_SITE='cross-site').status_code, 403)
        self.assertEqual(SitePageView.objects.count(), 0)

    def test_same_origin_referer_fallback(self):
        self.assertEqual(self.send(HTTP_ORIGIN='', HTTP_REFERER='http://testserver/portal/').status_code, 204)

    def test_invalid_payloads(self):
        for data in [[], None, {'path': '/'}, {**self.payload, 'event_id': 'bad'},
                     {**self.payload, 'path': '//evil.example/'}, {**self.payload, 'path': '/a\n'},
                     {**self.payload, 'path': '/' + 'a' * 513}, {**self.payload, 'referrer': []}]:
            if data is None:
                data = '无效内容'
            self.assertEqual(self.send(data).status_code, 400, data)

    def test_disabled_excluded_and_bots(self):
        config = SiteAnalyticsConfig.objects.create(pk=1, enabled=False)
        self.assertEqual(self.send().status_code, 204)
        config.enabled = True
        config.excluded_prefixes = ['/portal/']
        config.save()
        self.assertEqual(self.send().status_code, 204)
        config.excluded_prefixes = []
        config.save()
        self.assertEqual(self.send(HTTP_USER_AGENT='Googlebot').status_code, 204)
        self.assertEqual(SitePageView.objects.count(), 0)

    def test_dashboard_section_is_only_allowed_query_parameter(self):
        self.payload['path'] = '/dashboard/?section=site-analytics&password=secret'
        self.assertEqual(self.send().status_code, 204)
        self.assertEqual(SitePageView.objects.get().path, '/dashboard/?section=site-analytics')

    def test_rate_limit(self):
        cache.set('site-analytics:rate:' + digest('ip', '192.0.2.8'), 120, 60)
        self.assertEqual(self.send().status_code, 429)
        self.assertEqual(SitePageView.objects.count(), 0)

    def test_authentication_is_boolean_only(self):
        user = get_user_model().objects.create_user(username='统计测试用户')
        self.client.force_login(user)
        self.send()
        self.assertTrue(SitePageView.objects.get().authenticated)

    def test_ipv4_ipv6_and_proxy_chain(self):
        factory = RequestFactory()
        request = factory.post('/', REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR='198.51.100.99, 192.0.2.8, 10.0.0.1')
        self.assertEqual(client_ip(request, []), '10.0.0.2')
        self.assertEqual(client_ip(request, ['10.0.0.0/24']), '192.0.2.8')
        self.assertEqual(client_ip(factory.post('/', REMOTE_ADDR='::ffff:192.0.2.8'), []), '192.0.2.8')
        self.assertEqual(client_ip(factory.post('/', REMOTE_ADDR='2001:db8::1'), []), '2001:db8::1')
        self.assertEqual(client_ip(factory.post('/', REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR='bad'), ['10.0.0.0/24']), '10.0.0.2')

    def test_missing_migration_does_not_break_page(self):
        from django.db import OperationalError
        with patch('botend.analytics.views.config_value', side_effect=OperationalError('缺表')):
            self.assertEqual(self.send().status_code, 503)
        self.assertEqual(self.client.get('/auth/login/').status_code, 200)
        self.assertContains(self.client.get('/auth/login/'), 'data-site-analytics')


class AggregationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='统计管理员')
        group = DashboardUserGroup.objects.create(name='统计管理', permission_codes=['system.analytics'])
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.client.force_login(self.user)
        for day, visitor, ip, session, path, area in [
            (27, 'a', 'one', 's1', '/portal/news/', '前台'),
            (27, 'a', 'one', 's1', '/portal/news/', '前台'),
            (28, 'a', 'one', 's2', '/portal/article/1/', '前台'),
            (28, 'b', 'one', 's3', '/portal/news/', '前台'),
            (28, 'c', 'two', 's4', '/dashboard/', '后台'),
        ]:
            SitePageView.objects.create(event_id=uuid.uuid4(), occurred_at=timezone.make_aware(datetime(2026, 9, day, 0, 30)),
                visitor_key=visitor, ip_key=ip, session_key=session, path=path, area=area,
                referrer_host='example.org', device='电脑', browser='Chrome', os='Windows')

    def query(self, **params):
        return self.client.get(API, {'start': '2026-09-26', 'end': '2026-09-28', **params})

    def test_period_distinct_totals_and_frequency(self):
        response = self.query()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual({key: data['summary'][key] for key in ('pv', 'uv', 'ips', 'sessions')}, {'pv': 5, 'uv': 3, 'ips': 2, 'sessions': 4})
        self.assertEqual(data['summary']['pv_per_visitor'], 1.67)
        self.assertEqual(data['summary']['repeat_visitors'], 1)
        self.assertEqual([row['pv'] for row in data['daily']], [0, 2, 3])
        self.assertEqual([row['uv'] for row in data['daily']], [0, 1, 3])
        self.assertEqual(data['hourly'][0]['pv'], 5)
        self.assertEqual([row['count'] for row in data['visitor_frequency']], [2, 1, 0, 0])
        self.assertEqual([row['count'] for row in data['ip_frequency']], [1, 1, 0, 0])
        self.assertEqual(data['pages'][0]['label'], '/portal/news/')
        self.assertEqual(data['pages'][0]['pv'], 3)

    def test_filters_apply_to_all_aggregates(self):
        data = self.query(area='前台', path='/portal/news/').json()
        self.assertEqual(data['summary']['pv'], 3)
        self.assertEqual(data['summary']['uv'], 2)
        self.assertEqual(data['summary']['ips'], 1)
        self.assertEqual(len(data['pages']), 1)
        self.assertEqual(data['devices'][0]['pv'], 3)

    def test_timezone_changes_daily_and_hourly_boundaries(self):
        with timezone.override('UTC'):
            data = self.query().json()
        self.assertEqual([row['pv'] for row in data['daily']], [2, 3, 0])
        self.assertEqual(data['hourly'][16]['pv'], 5)

    def test_daylight_saving_repeated_hour(self):
        from datetime import timezone as utc_timezone
        for hour in (5, 6):
            SitePageView.objects.create(event_id=uuid.uuid4(), occurred_at=datetime(2026, 11, 1, hour, 30, tzinfo=utc_timezone.utc),
                visitor_key='dst', ip_key='dst', session_key='dst', path='/', area='前台', device='电脑', browser='Chrome', os='Windows')
        with timezone.override('America/New_York'):
            data = self.query(start='2026-10-31', end='2026-11-02').json()
        self.assertEqual([row['pv'] for row in data['daily']], [0, 2, 0])
        self.assertEqual(data['hourly'][1]['pv'], 2)

    def test_empty_and_invalid_ranges(self):
        data = self.query(path='/empty/').json()
        self.assertEqual(data['summary']['pv_per_visitor'], 0)
        self.assertEqual(data['summary']['uv'], 0)
        self.assertEqual(len(data['daily']), 3)
        self.assertEqual(data['pages'], [])
        for params in [{'start': 'bad'}, {'start': '2026-10-01'}, {'start': '2025-01-01'}, {'area': 'bad'}]:
            self.assertEqual(self.query(**params).status_code, 400)

    def test_unknown_ip_excluded_from_unique_counts(self):
        SitePageView.objects.update(ip_key='')
        data = self.query().json()
        self.assertEqual(data['summary']['ips'], 0)
        self.assertEqual(sum(row['count'] for row in data['ip_frequency']), 0)

    def test_permissions_cover_page_and_api(self):
        self.assertEqual(self.client.get('/dashboard/?section=site-analytics').status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(API).status_code, 403)
        self.client.force_login(get_user_model().objects.create_user(username='无权限', is_staff=True))
        self.assertEqual(self.client.get(API).status_code, 403)
        self.assertEqual(self.client.post(API, '{}', content_type='application/json').status_code, 403)
        self.assertEqual(self.client.get('/dashboard/?section=site-analytics').status_code, 403)

    def test_config_and_csrf(self):
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.user)
        config = {'enabled': False, 'excluded_prefixes': ['/auth/'], 'trusted_proxy_cidrs': ['10.0.0.2']}
        self.assertEqual(secured.post(API, json.dumps(config), content_type='application/json').status_code, 403)
        secured.get('/dashboard/?section=site-analytics')
        response = secured.post(API, json.dumps(config), content_type='application/json', HTTP_X_CSRFTOKEN=secured.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SiteAnalyticsConfig.objects.get(pk=1).trusted_proxy_cidrs, ['10.0.0.2/32'])
        self.assertFalse(self.query().json()['config']['enabled'])
        self.assertEqual(self.client.post(API, json.dumps({**config, 'trusted_proxy_cidrs': ['0.0.0.0/0']}), content_type='application/json').status_code, 400)

    def test_injection_registered_for_frontend_and_backend(self):
        for url in ['/', '/portal/news/', '/dashboard/?section=site-analytics']:
            response = self.client.get(url)
            self.assertContains(response, 'data-site-analytics', count=1)
