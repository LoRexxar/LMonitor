"""验证身份归属、注册凭证隔离、外部故障和管理员审核边界。"""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from botend.models import BilibiliAccountBinding, BilibiliBindingChallenge, BilibiliBindingConfig
from botend.services.bilibili_binding import (
    BindingError, BilibiliCommentReader, CommentSourceError, match_comment, parse_dynamic_url,
)


User = get_user_model()
API = '/auth/bilibili/api/'
ADMIN = '/api/dashboard/bilibili-binding/'
SOURCE = 'botend.services.bilibili_binding.BilibiliCommentReader.find'
DYNAMIC = 'https://www.bilibili.com/opus/123456789012345678'


@override_settings(ALLOW_REGISTRATION=True)
class BilibiliBindingTests(TestCase):
    def setUp(self):
        self.config = BilibiliBindingConfig.objects.create(pk=1, enabled=True, dynamic_url=DYNAMIC)
        self.user = User.objects.create_user(username='绑定用户', password='Binding-test-123!')
        self.admin = User.objects.create_superuser(username='绑定管理员', email='admin@example.com', password='Admin-test-123!')
        self.staff = User.objects.create_user(username='普通员工', password='Staff-test-123!', is_staff=True)

    def post(self, client, data, path=API):
        return client.post(path, json.dumps(data), content_type='application/json')

    def create(self, client=None, uid='12345'):
        response = self.post(client or self.client, {'action': 'create', 'uid': uid})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['challenge']

    def verify(self, challenge, client=None):
        with patch(SOURCE, return_value={'comment_id': '987654321012345678', 'nickname': '测试舰长'}):
            response = self.post(client or self.client, {'action': 'check', 'challenge_id': challenge['id']})
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_existing_account_complete_flow_and_replay(self):
        self.client.force_login(self.user)
        challenge = self.create()
        self.verify(challenge)
        self.assertFalse(BilibiliAccountBinding.objects.exists())
        response = self.post(self.client, {'action': 'confirm', 'challenge_id': challenge['id']})
        self.assertEqual(response.status_code, 200)
        binding = BilibiliAccountBinding.objects.get(user=self.user)
        self.assertEqual(binding.uid, '12345')
        self.assertEqual(binding.comment_id, '987654321012345678')
        self.assertEqual(self.post(self.client, {'action': 'confirm', 'challenge_id': challenge['id']}).status_code, 409)
        self.assertEqual(self.client.get(API).json()['binding']['uid'], '12345')

    def test_different_browser_cannot_read_check_or_claim_proof(self):
        challenge = self.create()
        attacker = Client()
        self.assertIsNone(attacker.get(API).json()['challenge'])
        for action in ('check', 'manual', 'confirm', 'cancel'):
            with self.subTest(action=action):
                self.assertEqual(self.post(attacker, {'action': action, 'challenge_id': challenge['id']}).status_code, 404)

    def test_login_identity_change_invalidates_anonymous_challenge(self):
        challenge = self.create()
        self.client.force_login(self.user)
        self.assertEqual(self.post(self.client, {'action': 'check', 'challenge_id': challenge['id']}).status_code, 403)

    def test_duplicate_identity_rejected_for_another_account(self):
        BilibiliAccountBinding.objects.create(user=self.user, uid='12345', evidence_url=DYNAMIC, comment_id='123')
        self.assertEqual(self.post(self.client, {'action': 'create', 'uid': '12345'}).status_code, 409)

    def test_uid_change_not_silently_allowed(self):
        BilibiliAccountBinding.objects.create(user=self.user, uid='12345', evidence_url=DYNAMIC, comment_id='123')
        self.client.force_login(self.user)
        self.assertEqual(self.post(self.client, {'action': 'create', 'uid': '54321'}).status_code, 409)

    def test_required_registration_cannot_bypass_verification(self):
        self.config.require_for_registration = True
        self.config.save()
        response = self.post(self.client, self.registration_data(), '/auth/register/')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='新用户').exists())

    def registration_data(self, challenge_id=''):
        return {'username': '新用户', 'email': 'new@example.com', 'password': 'New-user-123!',
                'confirm_password': 'New-user-123!', 'bilibili_challenge_id': challenge_id}

    def test_verified_registration_consumes_and_binds_atomically(self):
        self.config.require_for_registration = True
        self.config.save()
        challenge = self.create()
        self.verify(challenge)
        response = self.post(self.client, self.registration_data(challenge['id']), '/auth/register/')
        self.assertEqual(response.json()['status'], 'success', response.content)
        user = User.objects.get(username='新用户')
        self.assertEqual(user.bilibili_binding.uid, '12345')
        self.assertEqual(BilibiliBindingChallenge.objects.get(pk=challenge['id']).status, 'consumed')

    def test_registration_failure_does_not_consume_proof(self):
        challenge = self.create()
        self.verify(challenge)
        data = self.registration_data(challenge['id'])
        data['confirm_password'] = '不一致'
        self.assertEqual(self.post(self.client, data, '/auth/register/').json()['status'], 'error')
        self.assertEqual(BilibiliBindingChallenge.objects.get(pk=challenge['id']).status, 'verified')

    def test_logged_in_user_cannot_transfer_proof_to_new_registration(self):
        self.client.force_login(self.user)
        challenge = self.create()
        self.verify(challenge)
        self.assertEqual(self.post(self.client, self.registration_data(challenge['id']), '/auth/register/').status_code, 403)
        self.assertFalse(User.objects.filter(username='新用户').exists())

    def test_competing_registration_for_same_uid_rolls_back_user(self):
        first = self.create()
        other = Client(REMOTE_ADDR='127.0.0.2')
        second = self.create(other)
        self.verify(first)
        self.verify(second, other)
        self.post(self.client, self.registration_data(first['id']), '/auth/register/')
        data = self.registration_data(second['id'])
        data.update(username='第二用户', email='second@example.com')
        response = self.post(other, data, '/auth/register/')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(User.objects.filter(username='第二用户').exists())

    def test_optional_registration_remains_available(self):
        self.assertEqual(self.post(self.client, self.registration_data(), '/auth/register/').json()['status'], 'success')

    def test_closed_registration_blocks_anonymous_challenge(self):
        with override_settings(ALLOW_REGISTRATION=False):
            self.assertEqual(self.post(self.client, {'action': 'create', 'uid': '12345'}).status_code, 403)
            self.client.force_login(self.user)
            self.create()

    def test_source_failure_never_verifies_and_can_request_manual(self):
        challenge = self.create()
        with patch(SOURCE, side_effect=CommentSourceError()):
            response = self.post(self.client, {'action': 'check', 'challenge_id': challenge['id']})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(BilibiliBindingChallenge.objects.get(pk=challenge['id']).status, 'pending')
        response = self.post(self.client, {'action': 'manual', 'challenge_id': challenge['id']})
        self.assertEqual(response.json()['challenge']['status'], 'manual')

    def test_missing_comment_and_rate_limit(self):
        challenge = self.create()
        with patch(SOURCE, return_value=None) as reader:
            self.assertEqual(self.post(self.client, {'action': 'check', 'challenge_id': challenge['id']}).json()['challenge']['status'], 'pending')
            self.assertEqual(self.post(self.client, {'action': 'check', 'challenge_id': challenge['id']}).status_code, 429)
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.post(self.client, {'action': 'create', 'uid': '54321'}).status_code, 429)

    def test_expiration_and_configuration_change(self):
        challenge = self.create()
        BilibiliBindingChallenge.objects.filter(pk=challenge['id']).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post(self.client, {'action': 'check', 'challenge_id': challenge['id']}).status_code, 410)
        self.assertEqual(self.client.get(API).json()['challenge']['status'], 'expired')
        BilibiliBindingChallenge.objects.filter(pk=challenge['id']).update(expires_at=timezone.now() + timedelta(minutes=5))
        self.config.revision = uuid.uuid4()
        self.config.save()
        self.assertEqual(self.post(self.client, {'action': 'manual', 'challenge_id': challenge['id']}).status_code, 409)

    def test_config_change_during_network_read_cannot_accept_stale_proof(self):
        challenge = self.create()
        def change_config(*args):
            self.config.revision = uuid.uuid4()
            self.config.save()
            return {'comment_id': '123', 'nickname': '测试'}
        with patch(SOURCE, side_effect=change_config):
            self.assertEqual(self.post(self.client, {'action': 'check', 'challenge_id': challenge['id']}).status_code, 409)
        self.assertEqual(BilibiliBindingChallenge.objects.get(pk=challenge['id']).status, 'pending')

    def test_manual_review_requires_superuser_and_actual_evidence(self):
        self.client.force_login(self.user)
        challenge = self.create()
        self.post(self.client, {'action': 'manual', 'challenge_id': challenge['id']})
        data = {'action': 'approve', 'challenge_id': challenge['id'], 'note': '已核对真实评论', 'comment_id': '123'}
        self.assertEqual(self.post(self.client, data, ADMIN).status_code, 403)
        admin = Client()
        admin.force_login(self.admin)
        self.assertEqual(self.post(admin, data, ADMIN).status_code, 400)
        data['checked'] = True
        self.assertEqual(self.post(admin, data, ADMIN).status_code, 200)
        self.assertFalse(BilibiliAccountBinding.objects.exists())
        self.assertEqual(self.post(self.client, {'action': 'confirm', 'challenge_id': challenge['id']}).status_code, 200)
        self.assertEqual(self.user.bilibili_binding.verification_method, 'manual')

    def test_admin_access_and_config_validation(self):
        for user in (self.user, self.staff):
            self.client.force_login(user)
            self.assertEqual(self.client.get(ADMIN).status_code, 403)
            self.assertEqual(self.client.get('/dashboard/bilibili-binding/').status_code, 403)
        self.client.force_login(self.admin)
        fields = self.client.get(ADMIN).json()['config']
        fields['dynamic_url'] = 'https://127.0.0.1/internal'
        self.assertEqual(self.post(self.client, {'action': 'save', 'config': fields}, ADMIN).status_code, 400)
        fields['dynamic_url'] = 'https://t.bilibili.com/123456789012345678?spm_id=test'
        previous = self.config.revision
        fields['instructions'] = '请发表一级评论'
        self.assertEqual(self.post(self.client, {'action': 'save', 'config': fields}, ADMIN).status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.dynamic_url, DYNAMIC)
        self.assertNotEqual(previous, self.config.revision)

    def test_revoke_preserves_uid_and_invalidates_existing_proofs(self):
        self.client.force_login(self.user)
        challenge = self.create()
        self.verify(challenge)
        self.post(self.client, {'action': 'confirm', 'challenge_id': challenge['id']})
        binding = BilibiliAccountBinding.objects.get(user=self.user)
        admin = Client()
        admin.force_login(self.admin)
        self.assertEqual(self.post(admin, {'action': 'revoke', 'binding_id': binding.pk, 'note': '用户反馈需要重新核验'}, ADMIN).status_code, 200)
        binding.refresh_from_db()
        self.assertIsNotNone(binding.revoked_at)
        self.assertEqual(self.post(Client(), {'action': 'create', 'uid': binding.uid}).status_code, 409)

    def test_invalid_inputs_and_csrf_protection(self):
        for uid in ('0', '001', '-2', '昵称', '1' * 21, 12345, None):
            self.assertEqual(self.post(self.client, {'action': 'create', 'uid': uid}).status_code, 400)
        self.assertEqual(self.post(self.client, {'action': 'check', 'challenge_id': '不是编号'}).status_code, 404)
        self.assertEqual(self.post(self.client, []).status_code, 400)
        strict = Client(enforce_csrf_checks=True)
        strict.get(API)
        self.assertEqual(self.post(strict, {'action': 'create', 'uid': '12345'}).status_code, 403)

    def test_pages_and_user_management_expose_binding_without_secrets(self):
        self.client.force_login(self.user)
        response = self.client.get('/auth/bilibili/')
        self.assertContains(response, '绑定你的 B 站账号')
        self.assertIn('no-store', response['Cache-Control'])
        BilibiliAccountBinding.objects.create(user=self.user, uid='12345', evidence_url=DYNAMIC, comment_id='123')
        self.client.force_login(self.admin)
        response = self.client.get('/api/dashboard/users/?search=12345').json()
        self.assertEqual(response['data'][0]['bilibili_binding']['uid'], '12345')
        self.assertNotIn('owner_hash', json.dumps(self.client.get(ADMIN).json()))


class BilibiliReaderTests(SimpleTestCase):
    def setUp(self):
        self.challenge = BilibiliBindingChallenge(uid='12345', code='账号绑定 LM-TEST', dynamic_url=DYNAMIC,
            created_at=timezone.now() - timedelta(seconds=10), expires_at=timezone.now() + timedelta(minutes=5))
        self.reply = {'oid': '999', 'type': 17, 'root': 0, 'parent': 0, 'rpid_str': '987654321098765432',
                      'ctime': int(timezone.now().timestamp()), 'member': {'mid': '12345', 'uname': '测试'},
                      'content': {'message': '账号绑定 LM-TEST'}}

    def test_comment_requires_exact_sender_target_and_content(self):
        self.assertEqual(match_comment(self.reply, self.challenge, '999', 17)['comment_id'], '987654321098765432')
        variants = [dict(self.reply, oid='888'), dict(self.reply, type=11), dict(self.reply, root=1),
                    dict(self.reply, parent=1), dict(self.reply, member={'mid': '54321'}),
                    dict(self.reply, content={'message': '转发：账号绑定 LM-TEST'}), dict(self.reply, ctime=1)]
        for reply in variants:
            self.assertIsNone(match_comment(reply, self.challenge, '999', 17))

    def test_url_allowlist_blocks_redirects_and_other_hosts(self):
        for url in ['http://t.bilibili.com/123', 'https://b23.tv/test', 'https://www.bilibili.com.evil/opus/123',
                    'https://user@www.bilibili.com/opus/123', 'https://www.bilibili.com:443/opus/123',
                    'https://127.0.0.1/123', 'file:///etc/passwd', 'https://www.bilibili.com/opus/123/../456']:
            with self.subTest(url=url), self.assertRaises(BindingError):
                parse_dynamic_url(url)

    def test_dynamic_oid_is_resolved_not_guessed(self):
        reader = BilibiliCommentReader()
        data = {'item': {'id_str': '123456789012345678', 'basic': {'comment_id_str': '999', 'comment_type': 17}}}
        with patch.object(reader, '_get', side_effect=[data, {'replies': [self.reply]}]) as get:
            self.assertIsNotNone(reader.find(self.challenge, 5))
            self.assertEqual(get.call_args.args[1]['oid'], '999')

    def test_reader_pagination_is_bounded_and_continues(self):
        reader = BilibiliCommentReader()
        unrelated = dict(self.reply, member={'mid': '99999'})
        with patch.object(reader, 'resolve', return_value=('999', 17)), patch.object(reader, '_get', side_effect=[{'replies': [unrelated] * 20}, {'replies': [self.reply]}]) as get:
            self.assertIsNotNone(reader.find(self.challenge, 2))
            self.assertEqual(get.call_count, 2)
        with patch.object(reader, 'resolve', return_value=('999', 17)), patch.object(reader, '_get', return_value={'replies': [unrelated] * 20}) as get:
            self.assertIsNone(reader.find(self.challenge, 2))
            self.assertEqual(get.call_count, 2)

    def test_http_errors_and_malformed_responses_are_not_success(self):
        for status, body in [(412, {}), (302, {}), (200, {'code': -352}), (200, {'code': 0, 'data': None})]:
            with patch('botend.services.bilibili_binding.requests.get') as get:
                response = get.return_value.__enter__.return_value
                response.status_code = status
                response.iter_content.return_value = [json.dumps(body).encode()]
                with self.assertRaises(CommentSourceError):
                    BilibiliCommentReader().resolve(DYNAMIC)
                self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_malformed_comment_metadata_fails_closed(self):
        reader = BilibiliCommentReader()
        with patch.object(reader, '_get', return_value={'item': {'id_str': '123456789012345678', 'basic': ['错误格式']}}):
            with self.assertRaises(CommentSourceError):
                reader.resolve(DYNAMIC)
