import unittest
from botend.services.site_image_assets import source_url


class EditorialSourceTests(unittest.TestCase):
    def test_precise_public_image_paths(self):
        sources = (
            'https://wow.res.netease.com/images/20260724/1784862094195_11de472ce0.jpg',
            'https://nie.res.netease.com/r/pic/20260416/02d6ea3f-2e0a-4343-bd20-188ea331e158.jpg',
            'https://oss.shengnong.club/portal/articles/wowhead/specialty-dream-breath-382017/06cc1bf0d58a206d.gif',
            'https://assets-bwa.worldofwarcraft.blizzard.com/static/wow-icon-32x32.1a38d7c1c3d8df560d53f5c2ad5442c0401edf83.png',
            'https://img.youtube.com/vi/MXj5oa878G4/maxresdefault.jpg',
        )
        for u in sources:
            with self.subTest(u=u):
                self.assertEqual(source_url(u), u)
                legacy = u.replace('https:', 'http:')
                self.assertEqual(source_url(legacy), u if 'oss.shengnong.club/' in u else None)
                for bad in (u + '?signature=a', u + '#',
                            u.replace('https://', 'https://me@'), u.replace('.com/', '.com:443/'),
                            u.replace('/' + u.split('/')[-1], '/%2e%2e/' + u.split('/')[-1])):
                    if bad != u:
                        self.assertIsNone(source_url(bad))
        for bad in (sources[0].replace('/images/', '/users/'), sources[1].replace('/pic/', '/avatar/'),
                    sources[2].replace('/wowhead/', '/nga/'), sources[2].replace('382017/', 'draft/'),
                    sources[3].replace('wow-icon', 'portrait'), sources[4].replace('MXj5oa878G4', 'abcdefghijk')):
            self.assertIsNone(source_url(bad))

class DiscoveryTests(unittest.TestCase):
    def test_only_public_image_slots_from_indexed_editorial_rows(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from tools.discover_editorial_images import scan
        url = 'https://wow.res.netease.com/images/20260724/1784862094195_11de472ce0.jpg'
        other = url.replace('11de472ce0', 'aaaaaaaaaa')
        markup = (f'<picture><source srcset="{url} 2x"></picture><img src="{other}?secret">'
                  f'<section id="sec-nga"><img src="{other}"></section><video><source src="{other}"></video>'
                  f'<a href="{other}">link</a><img class="author-avatar" src="{other}">')
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            news = root / 'var/news'
            news.mkdir(parents=True)
            name = 'a' * 64 + '.json'
            (news / 'index.json').write_text(json.dumps({'shards': [{'file': name, 'count': 3}],
                'sources': {'nga': 1, 'unknown': 1, 'blizzard_cn': 1}, 'generation': 'fixture'}))
            (news / name).write_text(json.dumps({'items': [{'id': i, 'source': s}
                for i, s in enumerate(['nga', 'unknown', 'blizzard_cn'], 1)]}))
            cur = Mock()
            cur.fetchall.side_effect = [[(json.dumps([{'type': 'image', 'url': url, 'source_url': other}]),
                                          json.dumps([{'type': 'html', 'html': markup}]))], [], [(other,)]]
            report = scan(root, cur, 'fixture-date')
            self.assertEqual(report['refs'], {url: {'news:blizzard_cn': 2}})
            sql, params = cur.execute.call_args_list[0].args
            self.assertIn('is_active=1 AND source=%s AND id IN', sql)
            self.assertEqual(params, ('blizzard_cn', 3))
            self.assertEqual(cur.execute.call_args.args[1][0], 'fixture-date')
