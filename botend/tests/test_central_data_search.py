"""Real ORM regression coverage for branch-localized central-data searches."""
from django.http import QueryDict
from django.test import TestCase

from botend.dashboard.central_data import CentralDataAPI
from botend.models import (
    SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot, WowSpellSnapshot,
    WowTalentNodeMetadata, WowTalentVersion,
)


class CentralDataSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        season = SeasonMeta.objects.create(
            season_key='search-test', season_name='Search test',
            mplus_zone_id=1, raid_zone_id=2, gear_batch_key='current',
        )
        item = WowItemSnapshot.objects.create(
            item_id=268240, name='Fallback Sword', name_zh='正式长剑',
            simc_token='search_sword', slot_key='main_hand',
            metadata={'branch_display': {'ptr': {
                'name': 'Preview Blade', 'name_zh': '测试利刃', 'slot_key': 'off_hand',
            }}},
        )
        missing = WowItemSnapshot.objects.create(
            item_id=268241, name='Untranslated Sword', slot_key='main_hand',
        )
        for row in (item, missing):
            for branch in ('retail', 'ptr'):
                WowItemVariantSnapshot.objects.create(
                    item=row, season=season, batch_key='current', data_branch=branch,
                    variant_key=f'{row.item_id}-{branch}', variant_type='drop_equipment',
                    item_level=100,
                )
        for branch, raw_branch in (('retail', 'wow'), ('ptr', 'wowxptr')):
            version, _ = WowTalentVersion.objects.update_or_create(
                key=branch, defaults={'branch': branch, 'is_active': True, 'status': 'active'},
            )
            WowTalentNodeMetadata.objects.create(
                talent_version=version, class_name='Warrior', spec_name='Arms',
                node_id=990010, talent_id=990011, spell_id=990020,
                name='Fallback Strike', name_zh='节点斩击', description_zh='节点说明',
            )
            WowSpellSnapshot.objects.create(
                branch=raw_branch, locale='zhCN', spell_id=990020,
                display_data={'status': 'ready', 'default': {
                    'name_zh': '中央斩击' if branch == 'retail' else '测试斩击',
                    'name_en': 'Central Strike' if branch == 'retail' else 'Preview Strike',
                    'description_zh': '造成烈焰伤害' if branch == 'retail' else '造成冰霜伤害',
                }},
            )

    def assert_search(self, resource, branch, expected, **params):
        query = QueryDict('', mutable=True)
        query.update({'page_size': '25', **params})
        result = getattr(CentralDataAPI(), resource)(query, branch, None)
        self.assertEqual(result['pagination']['total'], len(expected))
        self.assertEqual([row['id'] for row in result['records']], expected)
        return result

    def test_items_search_localized_names_fallback_id_and_filters(self):
        cases = [
            ('retail', {'q': '268240'}, [268240]),
            ('retail', {'q': '正式长剑'}, [268240]),
            ('retail', {'q': 'fAlLbAcK sWoRd'}, [268240]),
            ('retail', {'q': 'search_sword'}, [268240]),
            ('retail', {'q': '测试利刃'}, []),
            ('ptr', {'q': '测试利刃'}, [268240]),
            ('ptr', {'q': 'pReViEw bLaDe'}, [268240]),
            ('ptr', {'q': '正式长剑'}, []),
            ('ptr', {'q': 'Fallback Sword'}, []),
            ('ptr', {'q': 'Untranslated Sword'}, [268241]),
            ('retail', {'q': '268240', 'slot': 'main_hand', 'language': 'translated'}, [268240]),
            ('ptr', {'q': '268240', 'slot': 'off_hand', 'language': 'translated'}, [268240]),
            ('ptr', {'q': '268240', 'slot': 'main_hand'}, []),
            ('retail', {'language': 'missing'}, [268241]),
            ('ptr', {'language': 'missing'}, [268241]),
            ('ptr', {'language': 'translated'}, [268240]),
            ('retail', {'q': 'not present'}, []),
        ]
        for branch, params, expected in cases:
            with self.subTest(branch=branch, params=params):
                self.assert_search('items', branch, expected, **params)

    def test_talents_search_json_and_columns_without_mixed_coalesce(self):
        for q in ('中央斩击', 'cEnTrAl sTrIkE', '烈焰', 'Fallback Strike',
                  '节点斩击', '节点说明', '990020', '990010', '990011'):
            with self.subTest(q=q):
                self.assert_search('talents', 'retail', [990020], q=q)
        self.assert_search('talents', 'retail', [], q='测试斩击')
        self.assert_search('talents', 'ptr', [990020], q='测试斩击')
        self.assert_search('talents', 'ptr', [990020], q='pReViEw sTrIkE')
        self.assert_search('talents', 'ptr', [990020], q='冰霜')
        self.assert_search('talents', 'ptr', [], q='中央斩击')
