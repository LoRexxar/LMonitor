"""Official Maxroll v9 oracle -> real ORM -> guide tooltip (no matching mocks).

Source: https://maxroll.gg/wow/class-guides/affliction-warlock-mythic-plus-guide
Official embed JS SHA256: 8d895399334c1e49759ff79edb1a828de5cc2b7cb19f4691d71d282e4a7e88cc
Map SHA256: bc37ac0ef380890bea3a41a2d04fbdd8256f5c20697a314ab871731e706760cd
sourcesContent[39] serialization.ts FormatVersion=9; [23] itemDecodeLink.
Node executed official module20 d/g: raw334 roundtrips exactly. raw342 is a
synthetic alternate level checked with that same independent official oracle.
ORM stats below are deliberately distinct synthetic sentinels, not live facts.
"""
from copy import deepcopy

from django.test import TestCase, SimpleTestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services import class_guide_codec as codec
from botend.services.class_guide_content import resolve_references
from botend.services.class_guide_icy import encode_item_variant, IcyPageConverter
from botend.services.class_guide_maxroll import convert
from botend.services.class_guide_markdown import blocks_to_markdown, compile_markdown
from botend.services.wow_item_display import load_item_tooltip_metadata

RAW334 = 'BgQAAkQACKgTBA'
RAW342 = 'BgQAAkQACKgVBA'
HEAD = 'DCRAAkQAnk7oQrjF2IoAYFA'
PAPERDOLL = 'JAoAImQA3_fABIgJEIIEBAwJ5OgCtOgF2IoAYFQApfBBAERAAcVrBQBBMWTBUwDukQgAIEAAXk7ACKgTBEQvJ8AAJ0wDsECqDQIABAwJqOgGAHQN4sUABkLJEIAEBAQBqOQ42UwME89FFIEApEwMMgVABASFzAQGRMDG7SCBAgQAAUAcgw9FEIICBAQ94GwTFAEBFnkPSAABE2SBwgBOyQVABQ1HZwABdfRBMUASIQvIEEAuA0TD2hyctQAAIEAA4IDVBA'
URL = 'https://maxroll.gg/wow/class-guides/affliction-warlock-mythic-plus-guide'


def token(code=RAW334, item_id=158368):
    return f'[[item:{item_id}@{code}]]'


def source(code=RAW334, item_id=158368):
    return {'kind': 'item', 'id': item_id, 'variant': code,
            'variant_format': 'maxroll-item-raw-v9', 'source_name': 'Test Item'}


class GuideItemVariantIdentityTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='guide-variant', season_name='Test',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='batch')
        self.item = WowItemSnapshot.objects.create(item_id=158368, name='Test Item',
            name_zh='中央装备名', icon='inv_helmet_01', catalog_type='equipment', slot_key='head',
            description_zh='旧参考装备\n+9999 智力')
        self.exact = self.variant('exact', 334, [642], 334)
        self.other = self.variant('alternate', 342, [642], 342)
        self.variant('wrong-highest', 999, [642], 999)
        self.variant('same-level-wrong-bonus', 334, [642, 999], 888)
        self.variant('wrong-branch', 334, [642], 777, data_branch='ptr')

    def variant(self, key, level, bonuses, amount, **kwargs):
        return WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
            batch_key='batch', variant_key=key, variant_type='drop_equipment',
            item_level=level, bonus_ids=bonuses, stats_json={'intellect': amount},
            **kwargs)

    def resolve(self, codes=(RAW334,), refs=None, **kwargs):
        return resolve_references([{'html': ' '.join(token(c) for c in codes)}], '12.1',
            source_refs=refs if refs is not None else {token(c): source(c) for c in codes}, **kwargs)

    def test_raw_official_items_select_exact_orm_facts_per_full_token(self):
        refs = self.resolve((RAW334, RAW342))
        for code, amount in ((RAW334, 334), (RAW342, 342)):
            ref = refs[token(code)]
            self.assertIn(f'+{amount} 智力', ref['tooltip_text'])
            self.assertNotIn('+999', ref['tooltip_text'])
            self.assertEqual(ref['name'], '中央装备名')
            self.assertEqual(ref['variant_status'], 'exact')
            self.assertEqual(ref['tooltip_source'], 'item_variant_snapshot')

    def test_no_exact_match_never_uses_highest_or_partial_bonus(self):
        self.exact.delete()
        ref = self.resolve()[token()]
        self.assertEqual(ref['variant_status'], 'unresolved')
        self.assertIn('未精确映射', ref['tooltip_text'])
        self.assertNotIn('智力', ref['tooltip_text'])
        self.assertEqual(ref['name'], '中央装备名')

    def test_zero_candidates_and_unknown_format_keep_names_not_reference_stats(self):
        WowItemVariantSnapshot.objects.all().delete()
        for refs in ({token(): source()}, {}, {token(): {**source(), 'variant_format': 'future-v99'}}):
            with self.subTest(refs=refs):
                ref = self.resolve(refs=refs)[token()]
                self.assertEqual(ref['variant_status'], 'unresolved')
                self.assertIn('未精确映射', ref['tooltip_text'])
                self.assertNotIn('9999', ref['tooltip_text'])
                self.assertEqual(ref['name'], '中央装备名')

    def test_conflicting_exact_rows_do_not_select_first(self):
        self.variant('conflict', 334, [642], 123)
        self.assertEqual(self.resolve()[token()]['variant_status'], 'unresolved')

    def test_legacy_source_provenance_selects_format_not_payload_guess(self):
        legacy = {token(): {k: v for k, v in source().items() if k != 'variant_format'}}
        self.assertEqual(self.resolve(refs=legacy, source_url=URL)[token()]['variant_status'], 'exact')
        self.assertEqual(self.resolve(refs={}, source_url=URL)[token()]['variant_status'], 'unresolved')
        self.assertEqual(self.resolve(refs=legacy, source_url='https://maxroll.gg.evil.test/wow/')[token()]['variant_status'], 'unresolved')
        self.assertEqual(self.resolve(refs=legacy)[token()]['variant_status'], 'unresolved')

    def test_icy_header_and_persisted_occurrences_are_not_raw(self):
        code = encode_item_variant([642], 90, 334)
        self.assertEqual(code, 'JAADBAgWCKgTBA')
        ref = {'kind': 'item', 'id': 158368, 'variant': code,
            'source_parameters': {'bonus': ['642'], 'level': ['90'], 'ilvl': ['334']},
            'source_occurrences': [{'source_url': 'https://www.icy-veins.com/wow/test-guide'}]}
        # Persisted Icy provenance wins over a legacy guide origin, not heuristics.
        resolved = self.resolve((code,), {token(code): ref}, source_url=URL)[token(code)]
        self.assertEqual(resolved['variant_status'], 'exact')
        self.assertIn('+334 智力', resolved['tooltip_text'])
        markup = '<div class="guide-page-content"><p><span data-iv-tooltip="wow/item/158368?bonus=642&amp;level=90&amp;ilvl=334">Test Item</span></p></div>'
        converter = IcyPageConverter(markup, 'https://www.icy-veins.com/wow/test-guide')
        converter.prepare()
        self.assertEqual(converter.refs[token(code)]['variant_format'], 'lmonitor-item-header-v9')

    def test_invalid_header_v9_never_claims_exact_orm_variant(self):
        for code in ('JAADBIgWCKgTBA', 'JAADBAgWCKgTBABBBB', 'IAADBAgWCKgTBA'):
            with self.subTest(code=code):
                ref = {**source(code), 'variant_format': codec.ITEM_HEADER_V9}
                resolved = self.resolve((code,), {token(code): ref})[token(code)]
                self.assertEqual(resolved['variant_status'], 'unresolved')
                self.assertEqual(resolved['variant_reason'], 'invalid_variant_payload')
                self.assertNotIn('智力', resolved['tooltip_text'])

    def test_extra_item_context_is_not_claimed_exact(self):
        for changes in ({'gems': [240906]}, {'modItems': [244007]}, {'modSpells': [123]},
                        {'context': 1}, {'customContext': {'playerLevel': 90}}, {'inheritStats': 0}):
            with self.subTest(changes=changes):
                item = codec.decode_item_variant_raw_v9(RAW334, 158368)
                item.update(changes)
                code = codec.encode_item_variant_raw_v9(item)
                ref = self.resolve((code,), {token(code): source(code)})[token(code)]
                self.assertEqual(ref['variant_status'], 'unresolved')
                self.assertEqual(ref['variant_reason'], 'unsupported_source_context')
                self.assertNotIn('智力', ref['tooltip_text'])

    def test_central_strict_mode_is_opt_in_and_requires_branch_level_full_bonus(self):
        request = dict(item_id=158368, item_level=334, bonus_ids=[642], data_branch='retail', strict_variant=True)
        exact = load_item_tooltip_metadata([request])[0]
        self.assertEqual(exact['variant_id'], self.exact.pk)
        for change in ({'bonus_ids': [642, 123]}, {'item_level': None}, {'data_branch': ''},
                       {'bonus_ids': [True]}, {'bonus_ids': [642, 642]}):
            with self.subTest(change=change):
                row = load_item_tooltip_metadata([{**request, **change}])[0]
                self.assertIsNone(row['variant_id'])
                self.assertFalse(row['tooltip_complete'])
                self.assertEqual(row['stats'], {})
        ordinary = load_item_tooltip_metadata([{'item_id': 158368, 'allow_default_variant': True}])[0]
        self.assertEqual(ordinary['item_level'], 999)


class GuideItemCodecTests(SimpleTestCase):
    def test_official_rich_context_roundtrip(self):
        # Synthetic full-field identity encoded/decoded by the pinned official JS,
        # not by our encoder; zero-valued fields must survive trimming too.
        code = 'Ty4BAkQAnk74UY8qQrjWCKQDOFw-HIArAAgrCAQAsCAABINBAIACOFQCaBAABwQ2CA'
        expected = {'id': 158368, 'modItems': [244007], 'modSpells': [12345678],
            'gems': [240906], 'bonuses': [642], 'specialization': 265,
            'level': 90, 'context': 13, 'itemLevel': 334, 'inheritStats': 0,
            'customContext': {'spells': [172, 686], 'auras': [172],
                'traitEntryRank': {1234: 2}, 'difficulty': 8, 'itemLevel': 334,
                'playerClass': 9, 'playerLevel': 90, 'playerGender': 0,
                'specializationIndex': 0, 'charData': {12: 345}}}
        self.assertEqual(codec.decode_item_variant_raw_v9(code, 158368), expected)
        self.assertEqual(codec.encode_item_variant_raw_v9(expected), code)

    def test_explicit_raw_and_header_entrypoints(self):
        decoded = codec.decode_item_variant_raw_v9(RAW334, 158368)
        self.assertEqual(decoded, {'id': 158368, 'specialization': 265,
            'modItems': [], 'modSpells': [], 'gems': [], 'bonuses': [642], 'itemLevel': 334})
        self.assertEqual(codec.encode_item_variant_raw_v9(decoded), RAW334)
        header = codec.decode_item_variant_header('JAADBAgWCKgTBA', 158368)
        self.assertEqual(header['level'], 90)
        self.assertEqual(header['itemLevel'], 334)
        self.assertEqual(header['bonuses'], [642])
        self.assertNotIn('specialization', header)

    def test_paperdoll_inherits_spec_and_preserves_full_item_and_source_refs(self):
        decoded = codec.decode_component('paperdoll', PAPERDOLL)
        item = decoded['items']['0']['item']
        self.assertEqual(item, {'id': 271874, 'specialization': 265,
            'modItems': [244007], 'modSpells': [], 'gems': [240906],
            'bonuses': [13846, 642], 'itemLevel': 344})
        self.assertEqual(codec.encode_item_variant_raw_v9(item), HEAD)
        post = {'gutenbergBlock': [{'blockName': 'core/embed', 'attributes': {
            'url': 'https://backend.maxroll.gg/wow/embed-tools/paperdoll=' + PAPERDOLL}}]}
        blocks, metadata = convert(post)
        full_token = token(HEAD, 271874)
        self.assertIn(full_token, blocks[0]['html'])
        self.assertEqual(metadata['source_refs'][full_token]['source_item'], item)
        self.assertEqual(metadata['source_refs'][full_token]['variant_format'], 'maxroll-item-raw-v9')
        self.assertIn('[[item:240906]]', blocks[0]['html'])

    def test_typed_inline_gets_marker_but_existing_children_are_not_rewritten(self):
        blocks, meta = convert({'gutenbergBlock': [{'blockName': 'core/paragraph',
            'innerHTML': f'<span class="wow-item" data-wow-item="158368:{RAW334}">Test Item</span>'}]})
        self.assertEqual(meta['source_refs'][token()]['variant_format'], 'maxroll-item-raw-v9')
        old = [{'id': 'gear', 'type': 'gear', 'data': {'source_code': PAPERDOLL},
                'children': [{'id': 'old', 'type': 'html', 'html': '<p>[[item:271874]] 人工正文</p>'}]}]
        compiled = compile_markdown(blocks_to_markdown(deepcopy(old)))
        self.assertIn('[[item:271874]]', str(compiled[0]['children']))
        self.assertNotIn('@', str(compiled[0]['children']))
