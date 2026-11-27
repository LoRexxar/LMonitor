import json
from django.test import RequestFactory, SimpleTestCase

from botend.common.api import (
    ApiError,
    BaseJsonAPIView,
    parse_bool,
    parse_bounded_int,
    parse_int,
    parse_json_body,
    parse_text,
    require_int,
)


class _SampleView(BaseJsonAPIView):
    error_prefix = '示例操作失败'

    def get(self, request):
        mode = parse_text(request.GET.get('mode'))
        if mode == 'api_error':
            raise ApiError('参数不合法', status=422, code='invalid_arg')
        if mode == 'crash':
            raise RuntimeError('boom')
        return self.ok({'page': parse_bounded_int(request.GET.get('page'), default=1, min_value=1, max_value=50)})

    def post(self, request):
        payload = parse_json_body(request)
        item_id = require_int(payload.get('id'), error='id参数错误')
        return self.ok({'id': item_id, 'flag': parse_bool(payload.get('flag'))})


class CommonApiUtilitiesTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view = _SampleView.as_view()

    def test_parsers_handle_edge_cases(self):
        self.assertEqual(parse_int('42'), 42)
        self.assertIsNone(parse_int(True))
        self.assertEqual(parse_int('bad', default=7), 7)
        self.assertEqual(parse_bounded_int('999', default=10, min_value=1, max_value=100), 100)
        self.assertEqual(parse_bounded_int('-5', default=10, min_value=1, max_value=100), 1)
        self.assertEqual(parse_bounded_int('abc', default=20, min_value=1, max_value=100), 20)
        self.assertTrue(parse_bool('true'))
        self.assertTrue(parse_bool('1'))
        self.assertFalse(parse_bool('0', default=True))
        self.assertTrue(parse_bool('unknown', default=True))
        self.assertEqual(parse_text('  hi  '), 'hi')
        self.assertEqual(parse_text(None, 'fallback'), 'fallback')

    def test_base_view_handles_ok_api_error_and_unexpected_crash(self):
        res = self.view(self.factory.get('/?page=3'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.content), {'success': True, 'data': {'page': 3}})

        res = self.view(self.factory.get('/?mode=api_error'))
        self.assertEqual(res.status_code, 422)
        self.assertEqual(json.loads(res.content), {'success': False, 'error': '参数不合法', 'code': 'invalid_arg'})

        res = self.view(self.factory.get('/?mode=crash'))
        self.assertEqual(res.status_code, 500)
        body = json.loads(res.content)
        self.assertFalse(body['success'])
        self.assertIn('示例操作失败', body['error'])

    def test_parse_json_body_and_require_int(self):
        req = self.factory.post('/', data=json.dumps({'id': '12', 'flag': 'yes'}), content_type='application/json')
        res = self.view(req)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.content), {'success': True, 'data': {'id': 12, 'flag': True}})

        bad_req = self.factory.post('/', data='not-json', content_type='application/json')
        res = self.view(bad_req)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.content)['success'])

        bad_id = self.factory.post('/', data=json.dumps({'id': 'abc'}), content_type='application/json')
        res = self.view(bad_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(json.loads(res.content)['error'], 'id参数错误')
