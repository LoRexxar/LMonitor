import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import alibabacloud_oss_v2 as oss
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from botend.services.simc_agent_oss import (
    ReportLeaseExpiredError,
    ReportStorageError,
    ReportValidationError,
    download_report_html,
    issue_upload_ticket,
    public_legacy_report_url,
    public_report_url,
    verify_uploaded_report,
)


class SimcAgentOSSTests(SimpleTestCase):
    @staticmethod
    def run_stub():
        return SimpleNamespace(
            pk=17,
            task_id=9,
            task=SimpleNamespace(pk=9, result_file='ignored-legacy-name.html'),
        )

    @override_settings(
        OSS_CONFIG={'base_url': 'https://reports.example/base'},
        ALLOWED_HOSTS=['testserver'],
    )
    def test_public_report_url_requires_canonical_run_key(self):
        self.assertEqual(
            public_report_url('simc_agent_results/simc_task_9_run_17.html'),
            'https://reports.example/base/simc_agent_results/simc_task_9_run_17.html',
        )
        for invalid_key in ('../other.html', 'simc_agent_results/../other.html',
                            'simc_agent_results/report 1.html'):
            with self.subTest(invalid_key=invalid_key):
                with self.assertRaises(ReportStorageError):
                    public_report_url(invalid_key)

    @override_settings(
        OSS_CONFIG={'base_url': 'https://reports.example/base'},
        ALLOWED_HOSTS=['testserver'],
    )
    def test_legacy_report_url_uses_uploaded_basename_only(self):
        filename = 'a' * 32 + '_run_17.html'
        self.assertEqual(
            public_legacy_report_url(f'simc_results/{filename}'),
            f'https://reports.example/base/{filename}',
        )
        for invalid_path in ('../report.html', 'simc_results/../report.html',
                             'simc_results/not-a-run.html'):
            with self.subTest(invalid_path=invalid_path), self.assertRaises(ReportStorageError):
                public_legacy_report_url(invalid_path)

    def test_public_report_url_rejects_unsafe_base_url(self):
        key = 'simc_agent_results/simc_task_9_run_17.html'
        for base_url in (
            'http://reports.example', 'https://user:pass@reports.example',
            'https://reports.example/?token=x', 'https://reports.example/#fragment',
        ):
            with self.subTest(base_url=base_url), override_settings(
                    OSS_CONFIG={'base_url': base_url}):
                with self.assertRaises(ReportStorageError):
                    public_report_url(key)

    @override_settings(
        OSS_CONFIG={'base_url': 'https://wowdaily.cn'},
        ALLOWED_HOSTS=['wowdaily.cn'],
    )
    def test_public_report_url_rejects_application_origin(self):
        with self.assertRaisesRegex(ReportStorageError, 'separate origin'):
            public_report_url('simc_agent_results/simc_task_9_run_17.html')

    def test_public_report_url_fails_closed_for_wildcard_application_hosts(self):
        for allowed_hosts in (['*'], ['.wowdaily.cn']):
            with self.subTest(allowed_hosts=allowed_hosts), override_settings(
                OSS_CONFIG={'base_url': 'https://oss.wowdaily.cn'},
                ALLOWED_HOSTS=allowed_hosts,
            ), self.assertRaises(ReportStorageError):
                public_report_url('simc_agent_results/simc_task_9_run_17.html')

    def test_issue_upload_ticket_is_single_object_checksum_bound_https_put(self):
        now = timezone.now()
        client = MagicMock()
        client.presign.return_value = SimpleNamespace(
            method='PUT',
            url='https://bucket.example/signed',
            expiration=None,
            signed_headers={
                'content-type': 'text/html; charset=utf-8',
                'content-md5': 'MDEyMzQ1Njc4OUFCQ0RFRg==',
                'x-oss-meta-sha256': 'a' * 64,
                'x-oss-forbid-overwrite': 'true',
            },
        )
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')):
            ticket = issue_upload_ticket(
                self.run_stub(), size=16, sha256='a' * 64,
                content_md5='MDEyMzQ1Njc4OUFCQ0RFRg==',
                lease_fence='sha256:fence',
                lease_expires_at=now + timedelta(seconds=90),
            )
        request = client.presign.call_args.args[0]
        self.assertEqual(request.bucket, 'bucket')
        self.assertEqual(request.key, 'simc_agent_results/simc_task_9_run_17.html')
        self.assertEqual(request.content_length, 16)
        self.assertEqual(request.content_md5, 'MDEyMzQ1Njc4OUFCQ0RFRg==')
        self.assertEqual(request.metadata, {
            'sha256': 'a' * 64, 'lease-fence': 'sha256:fence',
        })
        lifetime = client.presign.call_args.kwargs['expires'].total_seconds()
        self.assertGreater(lifetime, 0)
        self.assertLessEqual(lifetime, 90)
        self.assertEqual(request.acl, 'public-read')
        self.assertTrue(request.forbid_overwrite)
        self.assertEqual(ticket['method'], 'PUT')
        self.assertEqual(ticket['object_key'], request.key)

    @override_settings(OSS_CONFIG={
        'access_key_id': 'fake-id', 'access_key_secret': 'fake-secret',
        'region': 'cn-hangzhou', 'bucket_name': 'fake-bucket',
        'endpoint': 'https://oss-cn-hangzhou.aliyuncs.com',
        'base_url': 'https://fake-bucket.oss-cn-hangzhou.aliyuncs.com',
    })
    def test_real_sdk_presign_binds_immutable_public_report_headers(self):
        ticket = issue_upload_ticket(
            self.run_stub(), size=16, sha256='a' * 64,
            content_md5='MDEyMzQ1Njc4OUFCQ0RFRg==',
            lease_fence='sha256:fence',
            lease_expires_at=timezone.now() + timedelta(seconds=90),
        )
        self.assertTrue(ticket['url'].startswith('https://'))
        self.assertEqual(ticket['method'], 'PUT')
        normalized_headers = {key.lower(): value for key, value in ticket['headers'].items()}
        self.assertEqual(normalized_headers['content-md5'], 'MDEyMzQ1Njc4OUFCQ0RFRg==')
        self.assertEqual(normalized_headers['x-oss-meta-sha256'], 'a' * 64)
        self.assertEqual(normalized_headers['x-oss-meta-lease-fence'], 'sha256:fence')
        self.assertEqual(normalized_headers['x-oss-forbid-overwrite'], 'true')
        self.assertEqual(normalized_headers['x-oss-object-acl'], 'public-read')

    def test_issue_upload_ticket_rejects_plaintext_presign(self):
        client = MagicMock()
        client.presign.return_value = SimpleNamespace(
            method='PUT', url='http://bucket.example/signed',
            expiration=None, signed_headers={},
        )
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')):
            with self.assertRaises(ReportStorageError):
                issue_upload_ticket(
                    self.run_stub(), size=16, sha256='a' * 64,
                    content_md5='MDEyMzQ1Njc4OUFCQ0RFRg==',
                    lease_fence='sha256:fence',
                    lease_expires_at=timezone.now() + timedelta(seconds=90),
                )

    def test_issue_upload_ticket_recomputes_lifetime_immediately_before_presign(self):
        lease_expires_at = timezone.now() + timedelta(seconds=90)
        signing_time = lease_expires_at - timedelta(seconds=12, microseconds=500000)
        client = MagicMock()
        client.presign.return_value = SimpleNamespace(
            method='PUT', url='https://bucket.example/signed',
            expiration=None, signed_headers={},
        )
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')), \
                patch('botend.services.simc_agent_oss.timezone.now', return_value=signing_time):
            issue_upload_ticket(
                self.run_stub(), size=16, sha256='a' * 64,
                content_md5='MDEyMzQ1Njc4OUFCQ0RFRg==', lease_fence='sha256:fence',
                lease_expires_at=lease_expires_at,
            )
        self.assertEqual(
            client.presign.call_args.kwargs['expires'], timedelta(seconds=12),
        )

    def test_issue_upload_ticket_rejects_lease_expired_before_presign(self):
        now = timezone.now()
        client = MagicMock()
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')), \
                patch('botend.services.simc_agent_oss.timezone.now', return_value=now):
            with self.assertRaises(ReportLeaseExpiredError):
                issue_upload_ticket(
                    self.run_stub(), size=16, sha256='a' * 64,
                    content_md5='MDEyMzQ1Njc4OUFCQ0RFRg==', lease_fence='sha256:fence',
                    lease_expires_at=now,
                )
        client.presign.assert_not_called()

    def test_verify_uploaded_report_checks_size_sha256_and_html(self):
        client = MagicMock()
        client.head_object.return_value = SimpleNamespace(
            content_length=16,
            metadata={'sha256': 'a' * 64, 'lease-fence': 'sha256:fence'},
            content_type='text/html; charset=utf-8',
        )
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')):
            verify_uploaded_report(
                object_key='simc_agent_results/simc_task_9_run_17.html',
                size=16, sha256='a' * 64, lease_fence='sha256:fence',
            )
            client.head_object.return_value.content_length = 15
            with self.assertRaisesRegex(ReportStorageError, 'size mismatch'):
                verify_uploaded_report(
                    object_key='simc_agent_results/simc_task_9_run_17.html',
                    size=16, sha256='a' * 64, lease_fence='sha256:fence',
                )

    def test_download_report_html_reads_exact_canonical_object(self):
        payload = '<html>报告</html>'.encode('utf-8')
        digest = hashlib.sha256(payload).hexdigest()
        body = MagicMock()
        body.iter_bytes.return_value = iter((payload[:7], payload[7:]))
        client = MagicMock()
        client.get_object.return_value = SimpleNamespace(
            content_length=len(payload),
            content_type='text/html; charset=utf-8',
            metadata={'sha256': digest, 'lease-fence': 'sha256:fence'},
            body=body,
        )
        key = 'simc_agent_results/simc_task_9_run_17.html'
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')):
            self.assertEqual(
                download_report_html(
                    key,
                    expected_size=len(payload),
                    expected_sha256=digest,
                    expected_lease_fence='sha256:fence',
                ),
                ('<html>报告</html>', digest),
            )
        request = client.get_object.call_args.args[0]
        self.assertEqual(request.bucket, 'bucket')
        self.assertEqual(request.key, key)
        body.__enter__.assert_called_once_with()
        body.iter_bytes.assert_called_once_with()
        body.read.assert_not_called()
        body.__exit__.assert_called_once()

    def test_download_report_html_rejects_wrong_identity_or_size(self):
        client = MagicMock()
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')):
            for key, size in (
                ('../other.html', 16),
                ('simc_agent_results/simc_task_9_run_17.html', 0),
            ):
                with self.subTest(key=key, size=size), self.assertRaises(ReportValidationError):
                    download_report_html(
                        key,
                        expected_size=size,
                        expected_sha256='a' * 64,
                        expected_lease_fence='sha256:fence',
                    )
        client.get_object.assert_not_called()

        payload = b'<html>ok</html>'
        body = MagicMock()
        client.get_object.return_value = SimpleNamespace(
            content_length=len(payload) + 1,
            content_type='text/html; charset=utf-8',
            metadata={'sha256': hashlib.sha256(payload).hexdigest(), 'lease-fence': 'sha256:fence'},
            body=body,
        )
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')):
            with self.assertRaisesRegex(ReportValidationError, 'size mismatch'):
                download_report_html(
                    'simc_agent_results/simc_task_9_run_17.html',
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_lease_fence='sha256:fence',
                )
        body.__exit__.assert_called_once()

    def test_download_report_html_stops_when_stream_exceeds_verified_size(self):
        payload = b'<html>ok</html>'
        digest = hashlib.sha256(payload).hexdigest()
        body = MagicMock()
        body.iter_bytes.return_value = iter((payload, b'unexpected-extra-bytes'))
        client = MagicMock()
        client.get_object.return_value = SimpleNamespace(
            content_length=len(payload),
            content_type='text/html; charset=utf-8',
            metadata={'sha256': digest, 'lease-fence': 'sha256:fence'},
            body=body,
        )
        with patch('botend.services.simc_agent_oss._client', return_value=(oss, client, 'bucket')):
            with self.assertRaisesRegex(ReportValidationError, 'body size mismatch'):
                download_report_html(
                    'simc_agent_results/simc_task_9_run_17.html',
                    expected_size=len(payload),
                    expected_sha256=digest,
                    expected_lease_fence='sha256:fence',
                )
        body.__exit__.assert_called_once()

    def test_verify_uploaded_report_classifies_missing_object_as_validation_failure(self):
        class MissingObjectError(Exception):
            status_code = 404
            code = 'NoSuchKey'

        oss, client = MagicMock(), MagicMock()
        client.head_object.side_effect = MissingObjectError('missing')
        with patch(
            'botend.services.simc_agent_oss._client',
            return_value=(oss, client, 'bucket'),
        ):
            with self.assertRaisesRegex(ReportValidationError, 'does not exist'):
                verify_uploaded_report(
                    object_key='simc_agent_results/simc_task_9_run_17.html',
                    size=16, sha256='a' * 64, lease_fence='sha256:fence',
                )
