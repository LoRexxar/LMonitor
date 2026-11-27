import io
import unittest
from unittest.mock import Mock
from PIL import Image
from tools import publish_local_static as p


class StaticPublisherTest(unittest.TestCase):
    def test_gate_before_io(self):
        for name in ('/media/private.jpg', 'static/portal/images/../../secret.jpg', 'http://127.0.0.1/a.png', 'static/portal/favicons/unreviewed.ico'):
            self.assertIsNone(p.rule(name, '0' * 64))
        self.assertIsNone(p.rule('static/portal/images/portal-logo.webp', '0' * 64))

    def test_previous_receipt_requires_matching_digest_and_key(self):
        body = b'published'; name = 'static/wow_icons/small/test.jpg'; digest = p.sha(body)
        pair = ['wow_icons_oss/small/test.jpg', digest]
        self.assertEqual(p.previous_pair(name, body, {'/' + name:pair}), pair)
        for bad in (['wow_icons_oss/large/test.jpg', digest], ['https://private.invalid', digest], [pair[0], '0'*64]):
            self.assertIsNone(p.previous_pair(name, body, {'/'+name:bad}))
        self.assertIsNone(p.previous_pair(name, b'changed', {'/'+name:pair}))

    def test_svg_rejects_active_markup(self):
        for node in (b'<script/>', b'<path fill="url(https://example.org)"/>', b'<path onload="alert(1)"/>'):
            with self.assertRaises(p.Gate):
                p.inspect_svg(b'<svg xmlns="http://www.w3.org/2000/svg">'+node+b'</svg>')

    def test_no_unproven_game_icon_or_unverified_receipt(self):
        data = io.BytesIO(); Image.new('RGB', (18, 18)).save(data, 'JPEG'); body = data.getvalue()
        store = Mock(); store.read.return_value = None
        fetch = Mock(return_value=(b'other', 'image/jpeg'))
        with self.assertRaises(p.Gate):
            p.deliver('static/wow_icons/small/example.jpg', body, store, True, fetch)
        store.create.assert_not_called()
        store.read.side_effect = [body]; fetch.return_value = (b'other', 'image/jpeg')
        with self.assertRaises(p.Gate):
            p.deliver('static/wow_icons/small/example.jpg', body, store, True, fetch)
        store.create.assert_not_called()
