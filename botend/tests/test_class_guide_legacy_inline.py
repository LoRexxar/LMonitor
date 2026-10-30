"""Source-normalization contract; no DB, translation or metadata lookup."""
from unittest import TestCase

from botend.services.class_guide_content import clean_html
from botend.services.class_guide_maxroll import convert


class LegacyInlineReferenceTests(TestCase):
    def test_typed_inline_source_contract(self):
        # Exact paragraph from frozen guides 142/b-112-1-8 and 143/b-110-1-8.
        source = '<html><head></head><body><p><strong><span class="wow-spell" data-wow-id="702">Curse of Weakness</span></strong> -- <em>Uses</em> [<em>wow-spell id=702]Curse of Weakness[/wow-spell] on your mouseover target or target. </em></p>\n</body></html>'
        expected = '<p><strong>[[spell:702]]</strong> -- <em>Uses</em> <em>[[spell:702]] on your mouseover target or target. </em></p>'
        with self.subTest(case='real-source-convert'):
            blocks, meta = convert({'gutenbergBlock': [{'blockName': 'core/paragraph', 'innerHTML': source}]})
            self.assertEqual(blocks[0]['html'], expected)
            self.assertEqual(meta['source_refs']['[[spell:702]]']['id'], 702)
            self.assertEqual(meta['source_refs']['[[spell:702]]']['source_name'], 'Curse of Weakness')

        for kind, canonical, suffix in [('spell', 'spell', ''), ('trait', 'talent', ''), ('item', 'item', ':Ab_9-z')]:
            with self.subTest(case=kind):
                refs = {}
                token = f'[[{canonical}:702' + ('@Ab_9-z' if suffix else '') + ']]'
                value = f'<p>before [<em>wow-{kind} id=702{suffix}]Name[/wow-{kind}] after</em></p>'
                actual = clean_html(value, refs)
                self.assertEqual(actual, f'<p>before <em>{token} after</em></p>')
                self.assertEqual(refs[token], {'kind': canonical, 'id': 702, 'source_name': 'Name', 'variant': 'Ab_9-z' if suffix else ''})
                self.assertEqual(clean_html(actual), actual)

        positives = [
            ('[wow-spell id=702]Name[/wow-spell]', '[[spell:702]]'),
            ('<strong>[wow-spell id=702]<em>Name</em>[/wow-spell] tail</strong>', '<strong><em>[[spell:702]]</em> tail</strong>'),
            ('[[spell:702]]', '[[spell:702]]'),
            ('<span class="wow-item" data-wow-item="702:Ab_9-z">Name</span>', '[[item:702@Ab_9-z]]'),
            ('<span class="wow-spell" data-wow-id="702">First[/wow-spell] and [wow-trait id=703]Second</span>', '[[spell:702]] and [[talent:703]]'),
        ]
        for source, expected in positives:
            with self.subTest(source=source):
                self.assertEqual(clean_html(source), expected)

        protected = [
            '<pre>[wow-spell id=702]Name[/wow-spell]</pre>',
            '<code>[<em>wow-spell id=702]Name[/wow-spell]</em></code>',
            r'\[wow-spell id=702]Name[/wow-spell]',
            '&#91;wow-spell id=702]Name[/wow-spell]',
            '[[wow-spell id=702]Name[/wow-spell]]',
            '[wow-spell id=0]Name[/wow-spell]',
            '[wow-spell id=0702]Name[/wow-spell]',
            '[wow-spell id=-702]Name[/wow-spell]',
            '[wow-spell id=７０２]Name[/wow-spell]',
            '[wow-spell id=unknown]Name[/wow-spell]',
            '[wow-spell id=702 id=703]Name[/wow-spell]',
            '[wow-spell id=702]Name[/wow-item]',
            '[wow-spell id=702]Name',
            '[wow-item id=702:bad.value]Name[/wow-item]',
            '[wow-spell id=702][wow-item id=703]Name[/wow-item][/wow-spell]',
            '[wow-spell id=702]<p>Name</p>[/wow-spell]',
            '[wow-spell id=702]<a href="/x">Name</a>[/wow-spell]',
            '[wow-spell id=702]<code>Name</code>[/wow-spell]',
            '[wow-spell id=702]<br>Name[/wow-spell]',
        ]
        for source in protected:
            with self.subTest(protected=source):
                refs = {}
                actual = clean_html(source, refs)
                self.assertNotIn('[[spell:', actual)
                self.assertNotIn('[[item:', actual)
                self.assertEqual(refs, {})
                self.assertEqual(clean_html(actual), actual)

        # Removed executable/form subtrees must not leak source identities.
        for tag in ('script', 'style', 'iframe', 'object', 'svg', 'form', 'button'):
            with self.subTest(removed_subtree=tag):
                refs = {}
                actual = clean_html(f'<{tag}>[wow-spell id=702]Name[/wow-spell]</{tag}>', refs)
                self.assertEqual(actual, '')
                self.assertEqual(refs, {})
