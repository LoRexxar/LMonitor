import json

from django.test import SimpleTestCase

from botend.services.simc_apl.localization_enrichment import (
    apply_localization_overrides,
    build_localization_overrides,
    exact_search_candidates,
    parse_wowhead_search_html,
    resolve_wowhead_search,
    search_queries_for_token,
    split_token_suffixes,
)


REVISION = '8' * 40
SOURCE_HASH = 'a' * 64


def fact(token, *, kind='action', name_zh='', spec='fury', class_name='warrior'):
    return {
        'scope': 'spec', 'class_name': class_name, 'spec': spec,
        'hero_tree': None, 'token': token, 'symbol_kind': kind,
        'spell_id': None, 'trait_id': None, 'source': 'simc_manifest',
        'identity_source': '', 'identity_reason': '', 'identity_candidates': [],
        'aliases': [], 'options': [], 'name_en': token, 'name_zh': name_zh,
        'localization_source': 'wowhead' if name_zh else '',
        'localization_status': 'ok' if name_zh else 'unbound',
        'metadata': {},
    }


def package(facts):
    return {
        'schema_version': 1,
        'package_type': 'simc_apl_localization_metadata',
        'source_payload_sha256': SOURCE_HASH,
        'simc_revision': REVISION,
        'game_build': '12.0.7.68974',
        'facts': facts,
        'counts': {
            'source_record_count': len(facts), 'fact_count': len(facts),
            'official_spec_count': 1, 'scope_counts': {'spec': len(facts)},
            'kind_counts': {}, 'bound_count': 0, 'unbound_count': len(facts),
            'localized_count': sum(bool(row['name_zh']) for row in facts),
            'missing_zh_count': sum(not row['name_zh'] for row in facts),
        },
    }


class SimcAplWowheadSearchParserTests(SimpleTestCase):
    def test_parser_reads_only_spell_listview_json(self):
        spell_rows = [{
            'cat': -2, 'chrclass': 32, 'id': 47528,
            'name': 'Mind Freeze', 'searchpopularity': 478,
        }]
        item_rows = [{'id': 1, 'name': 'Mind Freeze'}]
        text = (
            '<script type="application/json" id="data.spells">'
            f'{json.dumps(spell_rows)}</script>'
            '<script type="application/json" id="data.items">'
            f'{json.dumps(item_rows)}</script>'
            '<script>new Listview({ template: "spell", id: "talents", '
            'data: WH.getPageData("spells"), });'
            'new Listview({ template: "item", id: "items", '
            'data: WH.getPageData("items"), });</script>'
        )

        self.assertEqual(parse_wowhead_search_html(text), [{
            'spell_id': 47528, 'name_en': 'Mind Freeze', 'listview': 'talents',
            'category': -2, 'class_mask': 32, 'search_popularity': 478,
        }])

    def test_exact_match_ignores_apostrophes_and_candidate_requires_unique_chinese(self):
        candidates = [{
            'spell_id': 439843, 'name_en': "Reaper's Mark", 'listview': 'talents',
            'category': -2, 'class_mask': 32, 'search_popularity': 499,
            'name_zh': '死神印记',
        }, {
            'spell_id': 999, 'name_en': "Reaper's Mark", 'listview': 'talents',
            'category': -2, 'class_mask': 4, 'search_popularity': 500,
            'name_zh': '错误职业名称',
        }]
        self.assertEqual(len(exact_search_candidates('reapers mark', candidates)), 2)
        resolved = resolve_wowhead_search('reapers mark', candidates, 'deathknight')
        self.assertEqual(resolved['name_zh'], '死神印记')
        self.assertEqual(resolved['candidate_spell_ids'], [439843])


class SimcAplLocalizationOverrideTests(SimpleTestCase):
    def test_suffix_and_query_normalization(self):
        self.assertEqual(split_token_suffixes('mid1_4pc_buff'),
                         ('mid1', ['4件套', '增益']))
        self.assertEqual(search_queries_for_token('reapers_mark_debuff'),
                         ('reapers mark debuff', 'reapers mark'))

    def test_builder_layers_catalog_control_wowhead_suffix_and_machine_sources(self):
        facts = [
            fact('bloodthirst', name_zh='嗜血'),
            fact('bloodthirst', spec='arms'),
            fact('run_action_list'),
            fact('reapers_mark', class_name='deathknight', spec='blood'),
            fact('bloodthirst_buff', kind='buff'),
            fact('internal_magic_counter', kind='buff'),
            fact('smite', class_name='priest', spec='shadow'),
        ]
        search_cache = {'records': {
            'reapers mark': {'candidates': [{
                'spell_id': 439843, 'name_en': "Reaper's Mark", 'name_zh': '死神印记',
                'listview': 'talents', 'category': -2, 'class_mask': 32,
                'search_popularity': 499,
            }]},
        }}
        translation_cache = {'records': {
            'internal magic counter': {'status': 'ok', 'name_zh': '内部魔法计数器'},
        }}
        overrides = build_localization_overrides(
            package(facts), search_cache=search_cache,
            translation_cache=translation_cache,
        )
        by_token = {row['token']: row for row in overrides['records']}

        self.assertEqual(by_token['bloodthirst']['localization_source'], 'catalog_exact')
        self.assertEqual(by_token['run_action_list']['name_zh'], '执行动作列表')
        self.assertEqual(by_token['reapers_mark']['name_zh'], '死神印记')
        self.assertEqual(by_token['reapers_mark']['localization_source'], 'wowhead_name_search')
        self.assertEqual(by_token['bloodthirst_buff']['name_zh'], '嗜血（增益）')
        self.assertEqual(by_token['internal_magic_counter']['name_zh'], '内部魔法计数器')
        self.assertEqual(by_token['smite']['name_zh'], '惩击')
        self.assertEqual(by_token['smite']['localization_source'], 'manual_semantic_dictionary')
        self.assertEqual(overrides['counts']['blank_count'], 0)

        enriched = apply_localization_overrides(package(facts), overrides)
        self.assertEqual(enriched['counts']['missing_zh_count'], 0)
        self.assertEqual(enriched['counts']['localized_count'], len(facts))
        self.assertIn('override_sha256', enriched['localization_enrichment'])
