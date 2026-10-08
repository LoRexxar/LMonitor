"""Offline signing and explicitly simulated OSS response-contract tests.

No test claims to upload to OSS or validate bucket/RAM policy in production.
"""
import base64
import hashlib
import io
import unittest
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit, parse_qs
from contextlib import ExitStack
import requests
from types import SimpleNamespace
from unittest.mock import patch
from django.test import override_settings, SimpleTestCase
from django.utils import timezone
from botend.services import simc_conditional_evidence_storage as storage
from simc_conditional_evidence import encode

CONFIG = dict(access_key_id='offline-id', access_key_secret='offline-secret', region='cn-beijing', bucket_name='offline-test')
FENCE = 'sha256$' + hashlib.sha256(b'offline-lease-token').hexdigest()
OTHER_FENCE = 'sha256$' + hashlib.sha256(b'other-offline-token').hexdigest()
NOW = datetime(2026, 10, 5, 0, 0, tzinfo=dt_timezone.utc)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class Body:
    def __init__(self, data): self.data = data; self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def iter_bytes(self, *, block_size):
        for i in range(0, len(self.data), block_size): yield self.data[i:i+block_size]


@override_settings(OSS_CONFIG=CONFIG)
class EvidenceStorageTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(storage.timezone, 'now', return_value=NOW))
        self.stack.enter_context(patch('alibabacloud_oss_v2.signer.v4.datetime',
            SimpleNamespace(datetime=FixedDatetime, timedelta=timedelta, timezone=dt_timezone)))
        self.run_record = SimpleNamespace(pk=23, task_id=17)
        self.payload = encode('private input', '{"sim":{}}')
        self.args = dict(size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest(), lease_fence=FENCE)
        self.key = storage.object_key_for_run(self.run_record, lease_fence=FENCE)

    def ticket(self, **changes):
        args = dict(self.args, content_md5=base64.b64encode(hashlib.md5(self.payload).digest()).decode(),
                    lease_expires_at=NOW + timedelta(seconds=45))
        args.update(changes)
        return storage.issue_upload_ticket(self.run_record, **args)

    def test_real_sdk_offline_signature_binds_size(self):
        ticket = self.ticket()
        query = parse_qs(urlsplit(ticket['url']).query)
        self.assertEqual(query['x-oss-additional-headers'], ['content-length'])
        self.assertEqual(query['x-oss-date'], ['20261005T000000Z'])
        self.assertEqual(query['x-oss-expires'], ['45'])
        self.assertEqual(ticket['expires_at'], (NOW + timedelta(seconds=45)).isoformat())
        headers = {k.lower(): v for k, v in ticket['headers'].items()}
        self.assertEqual(headers, {'content-length': str(len(self.payload)),
            'content-type': 'application/gzip',
            'content-md5': base64.b64encode(hashlib.md5(self.payload).digest()).decode(),
            'x-oss-object-acl': 'private', 'x-oss-forbid-overwrite': 'true',
            'x-oss-meta-sha256': self.args['sha256'], 'x-oss-meta-lease-fence': FENCE,
            'x-oss-meta-evidence-version': '1'})
        self.assertNotIn('offline-lease-token', str(headers))
        # Change ONLY size, keep MD5, key, credentials and clock identical.
        other = self.ticket(size=len(self.payload) + 1)
        other_query = parse_qs(urlsplit(other['url']).query)
        self.assertNotEqual(query.pop('x-oss-signature'), other_query.pop('x-oss-signature'))
        self.assertEqual(query, other_query)
        # Actual requests wire preparation, no mocked presign or network upload.
        prepared = requests.Request(ticket['method'], ticket['url'],
                                     headers=ticket['headers'], data=self.payload).prepare()
        self.assertEqual(prepared.body, self.payload)
        self.assertEqual(prepared.headers['Content-Length'], str(len(prepared.body)))
        self.assertNotIn('Transfer-Encoding', prepared.headers)
        self.assertTrue(prepared.url.startswith('https://'))
        for k, v in ticket['headers'].items():
            self.assertEqual(prepared.headers[k], v)
        cap = self.ticket(lease_expires_at=NOW + timedelta(hours=1))
        self.assertEqual(parse_qs(urlsplit(cap['url']).query)['x-oss-expires'], ['900'])

    def test_invalid_uploads_never_issue_ticket(self):
        for changes in [dict(size=0), dict(size=True), dict(size=storage.MAX_COMPRESSED_BYTES+1),
                dict(sha256='bad'), dict(content_md5='bad'), dict(lease_fence='offline-lease-token'),
                dict(lease_fence=FENCE.upper()), dict(lease_fence=FENCE+'\n'),
                dict(lease_expires_at=NOW), dict(lease_expires_at=None),
                dict(lease_expires_at=NOW.replace(tzinfo=None))]:
            with self.subTest(changes=list(changes)), self.assertRaises(storage.ReportStorageError):
                self.ticket(**changes)
        with override_settings(OSS_CONFIG=dict(CONFIG, endpoint='http://oss-cn-beijing.aliyuncs.com')):
            with self.assertRaises(storage.ReportStorageError): self.ticket()
        # Removing the adapter reproduces the real SDK gap, still fail-closed.
        from botend.services.simc_agent_oss import _client
        with patch.object(storage, '_private_client', side_effect=_client):
            with self.assertRaises(storage.ReportStorageError): self.ticket()

    def test_reject_corrupt_signing_results(self):
        oss, client, bucket = storage._private_client()
        real_presign = client.presign
        def corrupt(kind):
            def presign(request, **kwargs):
                result = real_presign(request, **kwargs)
                if kind == 'http': result.url = result.url.replace('https:', 'http:', 1)
                elif kind == 'expiry': result.expiration = NOW + timedelta(hours=1)
                else:
                    name, value = kind
                    if value is None: result.signed_headers.pop(name)
                    else: result.signed_headers[name] = value
                return result
            return presign
        mutations = ['http', 'expiry', ('Content-Length', None), ('Content-Length', '1'),
            ('x-oss-object-acl', 'public-read'), ('x-oss-forbid-overwrite', 'false'),
            ('Content-MD5', 'bad'), ('Content-Type', 'text/html'),
            ('x-oss-meta-lease-fence', 'offline-lease-token'), ('x-oss-meta-sha256', 'bad'),
            ('x-oss-meta-evidence-version', '2')]
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(client, 'presign', side_effect=corrupt(mutation)), \
                    patch.object(storage, '_private_client', return_value=(oss, client, bucket)):
                with self.assertRaises(storage.ReportStorageError): self.ticket()

    def fake_client(self, **changes):
        import alibabacloud_oss_v2 as oss
        headers = dict(content_length=len(self.payload), content_type='application/gzip', content_encoding=None, metadata={'sha256':self.args['sha256'], 'lease-fence':FENCE, 'evidence-version':'1'})
        headers.update(changes)
        self.body = Body(self.payload)
        self.get_count = 0
        def get(request):
            self.get_count += 1
            return SimpleNamespace(**headers, body=self.body)
        client = SimpleNamespace(head_object=lambda request: SimpleNamespace(**headers), get_object_acl=lambda request: SimpleNamespace(acl='private'), get_object=get)
        return oss, client, 'offline-test'

    def read(self, **changes):
        args = dict(object_key=self.key, expected_size=self.args['size'], expected_sha256=self.args['sha256'], expected_lease_fence=self.args['lease_fence'])
        args.update(changes)
        return storage.download_evidence(self.run_record, **args)

    def test_bounded_verified_download(self):
        with patch.object(storage, '_private_client', return_value=self.fake_client()):
            self.assertEqual(self.read(), {'prepared_input':'private input', 'report_json':'{"sim":{}}'})
        self.assertTrue(self.body.closed)

    def test_fail_closed_acl_metadata_fence_hash_and_size(self):
        for changes in [dict(content_length=0), dict(content_type='text/html'), dict(content_encoding='gzip'), dict(metadata={})]:
            with patch.object(storage, '_private_client', return_value=self.fake_client(**changes)):
                with self.assertRaises(storage.ReportValidationError): self.read()
                self.assertEqual(self.get_count, 0)
        sdk, client, bucket = self.fake_client()
        for acl in ['public-read', 'default', None]:
            client.get_object_acl = lambda request: SimpleNamespace(acl=acl)
            with patch.object(storage, '_private_client', return_value=(sdk, client, bucket)):
                with self.assertRaises(storage.ReportValidationError): self.read()
                self.assertEqual(self.get_count, 0)
        def denied(request): raise PermissionError('secret URL must not escape')
        client.get_object_acl = denied
        with patch.object(storage, '_private_client', return_value=(sdk, client, bucket)):
            with self.assertRaises(storage.ReportStorageError) as caught: self.read()
            self.assertNotIn('secret', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
        for payload in [self.payload[:-1], self.payload + b'x', b'x' * len(self.payload)]:
            sdk_client = self.fake_client(); self.body.data = payload
            with patch.object(storage, '_private_client', return_value=sdk_client):
                with self.assertRaises(storage.ReportValidationError): self.read()
                self.assertTrue(self.body.closed)
        with self.assertRaises(storage.ReportValidationError): self.read(expected_lease_fence=OTHER_FENCE)
        self.assertNotEqual(self.key, storage.object_key_for_run(self.run_record, lease_fence=OTHER_FENCE))
