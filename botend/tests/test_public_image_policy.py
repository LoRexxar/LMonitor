from django.test import SimpleTestCase
from django.http import HttpResponse


class PublicImagePolicyTests(SimpleTestCase):
    def test_login_page_enforces_image_origins(self):
        response = self.client.get('/auth/login/')
        self.assertEqual(response.status_code, 200)
        self.assertIn("img-src 'self' https://oss.wowdaily.cn data: blob:", response.get('Content-Security-Policy', ''))

    def test_article_and_author_do_not_publish_unreceipted_provenance(self):
        from django.template.loader import render_to_string
        from botend.services.content_image_display import display_article_blocks
        base = 'https://oss.wowdaily.cn/example/profile.png'
        synthetic = base + '?x-oss-signature=test-only'
        card = render_to_string('shared/class_guide_author.html', {'author_profile': {'name': 'Example', 'avatar': synthetic}})
        self.assertNotIn('<img', card)
        for u in (synthetic, base):
            self.assertEqual(display_article_blocks([{'type': 'image', 'source_url': u}]), [{'type': 'image', 'url': ''}])

    def test_rich_article_embedded_raster_keeps_src_and_srcset(self):
        from botend.services.content_image_display import display_article_blocks
        gif = 'data:image/gif;base64,R0lGODlhAQABAAAAACwAAAAAAQABAAA='
        html = f'<a href="/story/"><strong>图</strong><img src="{gif}" srcset="{gif} 1x"></a>'
        self.assertEqual(display_article_blocks([{'type': 'html', 'html': html}])[0]['html'], html)

    def test_sandbox_is_not_relaxed(self):
        from botend.image_policy import public_image_policy
        original = "default-src 'none'; img-src data:; sandbox allow-scripts"
        reply = public_image_policy(lambda _: HttpResponse(headers={'Content-Security-Policy': original}))(None)
        self.assertEqual(reply['Content-Security-Policy'].split(', ')[0], original)
