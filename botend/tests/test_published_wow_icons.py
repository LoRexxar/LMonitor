from types import SimpleNamespace
from unittest.mock import Mock

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from botend.services.published_wow_icons import published_wow_icon_url
from tools.list_published_wow_icons import inventory


class PublishedWowIconTests(SimpleTestCase):
    def test_size_sensitive_first_html_and_private_miss(self):
        base = 'https://wow.zamimg.com/images/wow/icons/'
        file = 'spell_fire_flamebolt.jpg'
        source = base + 'small/' + file
        mirror = 'https://oss.wowdaily.cn/wow_icons_oss/small/' + file
        self.assertIn('src="' + mirror + '"', render_to_string('dashboard/combat_entity_icon.html', {'icon': source}))
        for miss in (base + 'medium/' + file, base + 'tiny/' + file, source + '?sig=1',
                     base + 'small/absent_fixture.jpg', '?cast_icon=133', None):
            self.assertEqual(published_wow_icon_url(miss), '')

    def test_complete_scoped_pagination_and_safe_regular_keys(self):
        def page(req):
            self.assertEqual(req.bucket, 'wowdail-cn')
            self.assertEqual(req.delimiter, '/')
            has_more = req.prefix.endswith('/small/') and not req.continuation_token
            names = ['resume.jpg'] if req.continuation_token else ['good.jpg', 'bad/x.jpg', 'bad?.jpg']
            return SimpleNamespace(name=req.bucket, prefix=req.prefix, is_truncated=has_more,
                                   next_continuation_token='next' if has_more else '', contents=[
                SimpleNamespace(key=req.prefix + n, size=100, object_type='Normal', storage_class='Standard') for n in names])
        client = Mock(list_objects_v2=Mock(side_effect=page))
        self.assertEqual(inventory(client)['groups'], {2: 'resume', 15: 'good'})
        self.assertEqual([c.args[0].prefix for c in client.list_objects_v2.call_args_list],
                         ['wow_icons_oss/' + size + '/' for size in ('tiny', 'small', 'small', 'medium', 'large')])
