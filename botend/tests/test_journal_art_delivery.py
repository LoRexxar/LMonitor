from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.template import Context, Template
from django.test import SimpleTestCase


class JournalArtDeliveryTests(SimpleTestCase):
    @patch('botend.portal.adventure_journal.journal_files.read_index', return_value={'art_ids': [17, 18]})
    @patch('botend.services.journal_media.http_session', side_effect=AssertionError('unexpected network'))
    def test_only_published_art_is_delivered(self, _session, _index):
        bare = '/portal/adventure-journal/art/17/'
        versioned = bare + '?v=2'
        cdn = 'https://oss.wowdaily.cn/site-images/v1/' + 'a' * 64 + '.webp'
        with patch('botend.portal.adventure_journal.display_image_url', return_value=cdn) as resolve:
            for url in (bare, versioned):
                self.assertRedirects(self.client.get(url), cdn, fetch_redirect_response=False)
                resolve.assert_called_with(versioned)
            self.assertEqual(self.client.get('/portal/adventure-journal/art/99/').status_code, 404)
        with TemporaryDirectory() as tmp, self.settings(BASE_DIR=tmp):
            cached = Path(tmp) / '.cache/adventure-journal-art/17-v2.webp'
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b'not an OSS receipt')
            with patch('botend.portal.adventure_journal.display_image_url', return_value=''):
                self.assertEqual(self.client.get(bare).status_code, 404)
        with patch('botend.services.site_image_assets.published_image_url', return_value=''):
            self.assertEqual(Template('{% load site_images %}{{ id|journal_art_url }}').render(Context({'id': 17})), '')
