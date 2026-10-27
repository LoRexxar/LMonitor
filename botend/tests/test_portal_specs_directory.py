from html.parser import HTMLParser

from django.test import TestCase
from django.urls import reverse

from botend.constants.wow import CLASS_SPEC_MAP, SPEC_ICON


class DirectoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href', '').startswith('/portal/spec/'):
            self.links.append(attrs['href'])
        if tag == 'img':
            self.images.append(attrs)


class PortalSpecsDirectoryTests(TestCase):
    def test_directory_renders_every_canonical_spec_including_devourer(self):
        response = self.client.get('/portal/specs/')
        self.assertEqual(response.status_code, 200)
        parsed = DirectoryParser()
        parsed.feed(response.content.decode())
        expected = [reverse('spec_detail_player', kwargs={'class_name': cls, 'spec_name': spec})
                    for cls, specs in CLASS_SPEC_MAP.items() for spec in specs]
        self.assertCountEqual(parsed.links, expected)
        self.assertIn('/portal/spec/DemonHunter/Devourer/', parsed.links)
        self.assertContains(response, '噬灭')
        self.assertTrue(any(image.get('alt') == '噬灭' and
                            image.get('src') == SPEC_ICON['DemonHunter', 'Devourer']
                            for image in parsed.images))
