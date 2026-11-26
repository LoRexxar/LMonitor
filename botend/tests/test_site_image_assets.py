import socket
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from PIL import Image

from tools import publish_site_images as pub

from botend.services import site_image_assets as assets


class AssetPolicyTests(unittest.TestCase):
    def test_only_public_namespaces(self):
        url = 'https://wow.zamimg.com/images/wow/icons/small/spell_fire.jpg'
        self.assertEqual(assets.source_url(url), url)
        self.assertEqual(assets.published_image_url(url + '?token=private'), url + '?token=private')
        for invalid in (url + '?', url + '#', url + '?token=private', url.replace('wow.', 'user@wow.'),
                        url.replace('small/', 'small/../small/'), url.replace('/icons/', '/avatars/'),
                        url.replace('.com/', '.com.attacker/'), url.replace('spell_fire', '%73pell_fire')):
            self.assertIsNone(assets.source_url(invalid), invalid)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.manifest = self.root / 'map.json'
        self.manifest.write_text('{"schema":1,"images":{}}')
        self.state = self.root / 'state'
        self.src = 'https://wow.zamimg.com/images/wow/icons/small/test.jpg'
        self.list = self.root / 'urls'
        self.list.write_text(self.src + '\n' + self.src + '\n')
        b = BytesIO()
        Image.new('RGB', (18, 18), 'green').save(b, 'JPEG')
        self.data = b.getvalue()
        self.fetch = self.stack.enter_context(mock.patch.object(pub, 'fetch', return_value=(self.data, 'image/jpeg')))
        self.bucket = mock.Mock()
        self.objects = {}
        self.bucket.read.side_effect = self.objects.get
        self.bucket.create.side_effect = lambda k, b, m: self.objects.update({k: b})
        self.bucket_factory = self.stack.enter_context(mock.patch.object(pub, 'Bucket', return_value=self.bucket))
        self.stack.enter_context(redirect_stdout(StringIO()))
        self.stack.enter_context(redirect_stderr(StringIO()))

    def cli(self, *flags):
        return pub.main([*flags, '--manifest', str(self.manifest), '--state', str(self.state), str(self.list)])

    def test_cdn_failure_then_resume_without_new_source_or_put(self):
        self.assertEqual(self.cli(), 0)
        self.fetch.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.cli('--stage'), 0)
        self.assertEqual(self.cli('--stage'), 0)
        self.fetch.assert_called_once_with(self.src)
        self.assertEqual(self.cli('--existing'), 1)
        self.bucket.create.assert_not_called()
        self.fetch.return_value = (b'not the staged bytes', 'image/jpeg')
        self.assertEqual(self.cli('--apply'), 1)
        self.assertEqual(assets.read_records(self.manifest), {})
        self.fetch.return_value = (self.data, 'image/jpeg')
        self.assertEqual(self.cli('--apply'), 0)
        self.assertEqual(self.cli('--apply'), 0)
        k, digest = assets.read_records(self.manifest)[self.src]
        self.assertEqual(digest, pub.sha(self.data))
        self.bucket.create.assert_called_once_with(k, self.data, 'image/jpeg')
        self.fetch.assert_called_with(assets.CDN + k)
        with mock.patch.object(assets, 'MANIFEST', self.manifest):
            assets._published.cache_clear()
            self.addCleanup(assets._published.cache_clear)
            self.assertEqual(assets.published_image_url(self.src), assets.CDN + k)
            assets.published_image_map().clear()
            self.manifest.unlink()
            self.assertEqual(assets.published_image_url(self.src), assets.CDN + k)
            self.assertEqual(assets.published_image_url('/local'), '/local')

    def test_existing_canonical_must_be_same_bytes(self):
        self.assertEqual(self.cli('--stage'), 0)
        icon = assets.canonical_icon(self.src)[0]
        self.objects[icon] = b'not the same icon'
        self.assertEqual(self.cli('--existing'), 1)
        self.assertFalse(assets.read_records(self.manifest))
        self.objects[icon] = self.data
        self.assertEqual(self.cli('--existing'), 0)
        self.fetch.assert_called_with(assets.CDN + icon)
        self.bucket.create.assert_not_called()

    def test_rejection_precedes_fetch_and_state(self):
        self.list.write_text(self.src + '\n' + self.src + '?private=never-log-this\n')
        with self.assertRaisesRegex(pub.Gate, '^rejected input 1, line 2$'):
            self.cli('--apply')
        self.bucket_factory.assert_not_called()
        self.fetch.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_no_create_on_ambiguous_get_or_bad_staged_bytes(self):
        self.cli('--stage')
        self.bucket.read.side_effect = TimeoutError
        self.assertEqual(self.cli('--apply'), 1)
        self.bucket.create.assert_not_called()
        with closing(sqlite3.connect(self.state / 'site-images.sqlite3')) as c, c:
            c.execute('update stage set data=?', (b'corrupt',))
        self.bucket.read.reset_mock()
        self.assertEqual(self.cli('--apply'), 1)
        self.bucket.read.assert_not_called()
        self.bucket.create.assert_not_called()
        self.assertEqual(assets.read_records(self.manifest), {})

    def test_bad_source_type_is_not_staged(self):
        self.fetch.return_value = (self.data, 'image/svg+xml')
        self.assertEqual(self.cli('--stage'), 1)
        self.assertEqual(self.cli('--apply'), 1)
        self.bucket.read.assert_not_called()
        self.bucket.create.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_accepts_bounded_animation_and_rejects_excessive_work(self):
        frames = [Image.new('RGB', (7, 9), color) for color in ('red', 'green', 'blue')]
        with BytesIO() as buf:
            frames[0].save(buf, 'GIF', save_all=True, append_images=frames[1:])
            self.assertEqual(pub.inspect_image(buf.getvalue(), 'image/gif'), ('gif', 'image/gif', (7, 9)))
            with mock.patch.object(pub, 'MAX_PIXELS', 60), self.assertRaises(pub.Gate):
                pub.inspect_image(buf.getvalue(), 'image/gif')

    def test_nonpublic_address_fails_before_socket(self):
        u = 'https://wow.lorexxar.cn/favicon.ico'
        with mock.patch.object(socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), \
                mock.patch.object(socket, 'socket') as connect, self.assertRaisesRegex(pub.Gate, 'non-public-DNS'):
            pub.fetch(u)
        connect.assert_not_called()

    def test_http_alias_only_for_bili_public_video_covers(self):
        u = 'http://i1.hdslb.com/bfs/archive/' + 'a' * 40 + '.jpg'
        self.assertEqual(assets.source_url(u), 'https:' + u[5:])
        for bad in (u + '?', u.replace('archive', 'face'), u.replace('i1.', 'i9.'), u.replace('hdslb', 'hdslb.attacker')):
            self.assertIsNone(assets.source_url(bad))

    def test_sdk_create_is_conditional_and_only_exact_conflict_allowed(self):
        import alibabacloud_oss_v2 as oss
        s = pub.Bucket()
        s.writable = True
        s.client = mock.Mock()
        k = assets.PREFIX + 'a' * 64 + '.jpg'
        def error(status, code):
            details = dict(request_id='', message='', ec='', timestamp='', request_target='')
            return oss.exceptions.OperationError(name='test', error=oss.exceptions.ServiceError(status_code=status, code=code, **details))
        s.client.put_object.side_effect = error(409, 'FileAlreadyExists')
        s.create(k, b'abc', 'image/jpeg')
        req, = s.client.put_object.call_args.args
        self.assertIs(req.forbid_overwrite, True)
        self.assertEqual((req.bucket, req.key, req.content_md5, req.body),
                         (pub.BUCKET, k, 'kAFQmDzST7DWlj99KOF/cg==', b'abc'))
        s.client.put_object.side_effect = error(409, 'other')
        with self.assertRaises(oss.exceptions.OperationError):
            s.create(k, b'abc', 'image/jpeg')
        for code in ('NoSuchKey', 'NoSuchBucket'):
            s.client.get_object.side_effect = error(404, code)
            if code == 'NoSuchKey':
                self.assertIsNone(s.read(k))
            else:
                with self.assertRaises(oss.exceptions.OperationError):
                    s.read(k)

    def test_only_exact_empty_versioning_xml_passes(self):
        s = pub.Bucket()
        s.client = mock.Mock()
        out = s.client.invoke_operation.return_value
        out.status_code = 200
        out.http_response.content = b'<VersioningConfiguration />'
        s.require_unversioned()
        for body in (b'<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>', b'<NotVersioning/>'):
            out.http_response.content = body
            with self.assertRaises(pub.Gate):
                s.require_unversioned()
