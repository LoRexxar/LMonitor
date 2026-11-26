from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from botend.services import content_image_display as images


class DynamicImageDisplayTests(SimpleTestCase):
    def test_published_slots_only(self):
        source = 'https://example.org/a.jpg'
        oss = 'https://oss.wowdaily.cn/assets/p.jpg'
        node = f'<img alt="src={source}" data-source-src="{source}" src="{source}">'
        html = (f'<!--{node}--><script>let html=`{node}`</script><textarea>{node}</textarea>'
                f'<a href="{source}">{source}</a><video poster="{source}"><source src="{source}"></video>'
                f'<picture><source srcset="{source} 1x, /local.jpg 2x">{node}</picture>')
        expected = (html.replace(f'poster="{source}"', f'poster="{oss}"')
                    .replace(f'srcset="{source} 1x', f'srcset="{oss} 1x')
                    .replace(node + '</picture>', node.replace(f'-src="{source}"', f'-src="{oss}"').replace(f' src="{source}"', f' src="{oss}"') + '</picture>'))
        with patch.object(images, 'display_image_url', side_effect=lambda u: oss if u == source else u):
            self.assertEqual(images.display_image_html(html), expected)
            blocks = [{'type': 'html', 'html': html}, {'type': 'image', 'url': source, 'source_url': source},
                      {'type': 'paragraph', 'text': source}]
            display = images.display_article_blocks(blocks)
            self.assertEqual(display[0]['html'], expected)
            self.assertEqual(display[1], {'type': 'image', 'url': oss})
            self.assertEqual(display[2], blocks[2])
            self.assertEqual(blocks[1]['url'], source)

    def test_preload_svg_and_favicon_do_not_bypass_image_projection(self):
        raw = ('<link rel="preload" as="image" href="//elsewhere/image" imagesrcset="//elsewhere/image 1x">'
               '<link rel="icon" href="/unpublished.ico"><svg><image xlink:href="//elsewhere/image"/></svg>'
               '<input type="image" src="https://elsewhere/image"><a href="//elsewhere/image">x</a>')
        self.assertEqual(images.display_image_html(raw), '<link rel="preload" as="image"><link rel="icon">'
                         '<svg><image/></svg><input type="image"><a href="//elsewhere/image">x</a>')

    def test_misses_remove_only_image_slots_and_preserve_raw_inputs(self):
        absent = 'https://example.org/unpublished.png'
        oss = 'https://oss.wowdaily.cn/portal/articles/already-delivered.png'
        html = (f'<a href="{absent}">source</a><picture><source srcset="{absent} 2x, {oss} 1x">'
                f'<img src={absent} data-original="{absent}" alt="caption"></picture>')
        raw = [{'type': 'html', 'html': html, 'original_html': html},
               {'type': 'image', 'url': absent, 'source_url': absent}]
        display = images.display_article_blocks(raw)
        expected = (f'<a href="{absent}">source</a><picture><source srcset="{oss} 1x">'
                    '<img alt="caption"></picture>')
        self.assertEqual(display[0], {'type': 'html', 'html': expected, 'original_html': expected})
        self.assertEqual(display[1], {'type': 'image', 'url': ''})
        self.assertEqual(raw[0]['html'], html)
        self.assertEqual(raw[1]['source_url'], absent)

    def test_verified_mapping_applies_to_new_export_and_preexisting_report(self):
        from botend.portal.views import PortalReportFileView
        from botend.wow_daily_report.generator import render_daily_html

        source = 'https://i0.hdslb.com/bfs/archive/' + 'a' * 40 + '.jpg'
        oss = 'https://oss.wowdaily.cn/site-images/fixture.jpg'
        sections = [{'key': 'videos', 'title': '视频', 'items': [
            {'title': '视频', 'url': source, 'cover_url': source}]}]
        original = f'<a href="{source}"><img src="{source}"></a>'
        with patch('botend.services.site_image_assets._published', return_value={source: oss}):
            document = render_daily_html(date(2026, 11, 20), sections)
            self.assertIn(f'src="{oss}"', document)
            self.assertIn(f'href="{source}"', document)
            self.assertEqual(sections[0]['items'][0]['cover_url'], source)
            with TemporaryDirectory() as tmp, override_settings(BASE_DIR=tmp):
                path = Path(tmp, 'static/portal/reports/legacy.html')
                path.parent.mkdir(parents=True)
                path.write_text(original, encoding='utf-8')
                response = PortalReportFileView.as_view()(RequestFactory().get('/'), path.name)
                self.assertEqual(response.content.decode(), f'<a href="{source}"><img src="{oss}"></a>')
                self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_existing_guide_uses_original_byte_offsets_and_projects_uncached_private_header(self):
        from botend.portal.class_guides import PortalGuidePageMixin

        url, oss = 'https://example.org/public.jpg', 'https://oss.wowdaily.cn/p.jpg'
        before, header = f'<img src="{url}">攻略'.encode(), b'<header>Anonymous</header>'
        body = before + header + b'<article>info</article>'
        private = f'<header><img src="{url}"></header>'
        request = RequestFactory().get('/')
        request.user = SimpleNamespace(is_authenticated=True)
        with (patch('botend.portal.class_guides.read_guide_page', return_value=(
                body, (len(before), len(before) + len(header), len(body)))),
              patch('botend.portal.class_guides.render_to_string', return_value=private),
              patch('botend.portal.class_guides.has_dashboard_permission', return_value=False),
              patch('botend.services.site_image_assets._published', return_value={url: oss})):
            response = PortalGuidePageMixin().snapshot_response(request)
        self.assertEqual(response.content.decode(), f'<img src="{oss}">攻略<header><img src="{oss}"></header><article>info</article>')
