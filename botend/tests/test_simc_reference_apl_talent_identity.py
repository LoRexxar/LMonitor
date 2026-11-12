"""Real ORM regressions for runtime TraitNode -> DB2 Entry display identity.

Pillar/Breath identifiers are captured production relationships; choice and
collision rows are deliberately synthetic adversarial fixtures, not game data.
"""
from unittest.mock import patch

from django.test import TestCase

from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, WowSpellSnapshot,
    WowTalentNodeMetadata, WowTalentVersion,
)
from botend.services.simc_apl.readable import display_symbols
from botend.services.wow_spell_display import context_key


class AplTalentIdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.version = WowTalentVersion.objects.create(
            key='apl-identity-retail', branch='retail', is_active=True,
        )
        cls.ptr = WowTalentVersion.objects.create(
            key='apl-identity-ptr', branch='ptr', is_active=True,
        )

    def scope(self, token, trait_id, spell_id=None, *, class_name='deathknight', spec='frost'):
        symbol = SimcAplSymbol.objects.create(token=token, symbol_kind='talent')
        return SimcAplSymbolScope.objects.create(
            symbol=symbol, class_name=class_name, spec=spec,
            trait_id=trait_id, spell_id=spell_id,
            source='simc_manifest', identity_source='runtime_expression_resolver',
        )

    def node(self, trait_id, entry_id, spell_id, **overrides):
        values = dict(
            talent_version=self.version, class_name='DeathKnight', spec_name='Frost',
            talent_id=trait_id, node_id=entry_id, spell_id=spell_id,
            display_spell_id=spell_id, name_zh='权威天赋', icon='spell_deathknight_pillaroffrost',
            description_zh='节点原始说明。', source='db2_backfill',
        )
        values.update(overrides)
        return WowTalentNodeMetadata.objects.create(**values)

    def snapshot(self, spell_id, *, branch='wow', **overrides):
        values = dict(
            branch=branch, locale='zhCN', spell_id=spell_id, name_zh='中央技能',
            description='中央说明。', icon='spell_frost_frostbolt02',
        )
        values.update(overrides)
        return WowSpellSnapshot.objects.create(**values)

    def display(self, token, *, version=None, class_name='deathknight', spec='frost'):
        # Even failure paths may not invoke a network/producer during rendering.
        with patch('requests.sessions.Session.request', side_effect=AssertionError('read-only rendering')):
            rows = display_symbols(class_name, spec, version or self.version)
        return rows[('talent', token)]

    def assert_unresolved(self, item, token):
        self.assertIsNone(item['spell_id'])
        self.assertEqual(item['name'], token)
        self.assertEqual(item['icon'], '')
        self.assertEqual(item['description'], '当前资料尚未提供说明。')
        self.assertEqual(item['hero_tree_ids'], [])

    def test_traitnode_not_entry_restores_chinese_icon_and_entry_context(self):
        self.scope('pillar_of_frost', 101929, 51271)
        self.node(101929, 125874, 51271)
        self.snapshot(51271, display_data={
            'default': {'name_zh': '公共技能', 'description_zh': '公共说明', 'icon': 'inv_misc_questionmark'},
            'contexts': {
                context_key('DeathKnight', 'Frost', 125874): {
                    'name_zh': '冰霜之柱', 'description_zh': '真正 Entry 的冰霜之柱说明。',
                    'icon': 'spell_deathknight_pillaroffrost',
                },
                context_key('DeathKnight', 'Frost', 101929): {
                    'name_zh': '错误同号节点', 'description_zh': '不能用 TraitNode 当 Entry。',
                },
            },
        })
        item = self.display('pillar_of_frost')
        self.assertEqual(item['spell_id'], 51271)
        self.assertEqual(item['name'], '冰霜之柱')
        self.assertEqual(item['description'], '真正 Entry 的冰霜之柱说明。')
        self.assertIn('spell_deathknight_pillaroffrost', item['icon'])

    def test_same_number_entry_collision_is_not_a_traitnode_relation(self):
        self.scope('breath_of_sindragosa', 76093, 1249658)
        self.node(999999, 76093, 1249658, name_zh='同号 Entry 陷阱')
        self.snapshot(1249658, name_zh='不能直接按 runtime 法术绕过节点关系')
        self.assert_unresolved(self.display('breath_of_sindragosa'), 'breath_of_sindragosa')

    def test_real_breath_node_wins_over_colliding_entry(self):
        self.scope('breath_of_sindragosa', 76093, 1249658)
        self.node(76093, 96222, 1249658, name_zh='冰龙吐息', description_zh='正确节点说明。')
        self.node(999999, 76093, 999001, name_zh='错误节点')
        item = self.display('breath_of_sindragosa')
        self.assertEqual(item['spell_id'], 1249658)
        self.assertEqual(item['name'], '冰龙吐息')
        self.assertEqual(item['description'], '正确节点说明。')

    def test_choice_runtime_spell_disambiguates_regardless_of_row_order(self):
        for index, selected_first in enumerate((True, False)):
            with self.subTest(selected_first=selected_first):
                token, trait = f'choice_{index}', 900000 + index
                self.scope(token, trait, 51271)
                entries = [(910000 + index * 2, 51271, '已验证选项'),
                           (910001 + index * 2, 1249658, '另一选项')]
                for entry, sid, name in entries if selected_first else reversed(entries):
                    self.node(trait, entry, sid, name_zh=name)
                item = self.display(token)
                self.assertEqual(item['spell_id'], 51271)
                self.assertEqual(item['name'], '已验证选项')

    def test_choice_without_spell_id_keeps_conflicting_entries_unresolved(self):
        self.scope('ambiguous_choice', 900010)
        self.node(900010, 910010, 51271)
        self.node(900010, 910011, 1249658)
        self.assert_unresolved(self.display('ambiguous_choice'), 'ambiguous_choice')

    def test_choice_shared_spell_does_not_choose_arbitrary_entry_override(self):
        for index, runtime_sid in enumerate((None, 51271)):
            with self.subTest(runtime_sid=runtime_sid):
                token, trait = f'shared_spell_{index}', 900020 + index
                self.scope(token, trait, runtime_sid)
                self.node(trait, 910020 + index * 2, 51271, description_zh='第一 Entry override')
                self.node(trait, 910021 + index * 2, 51271, description_zh='第二 Entry override')
                self.assert_unresolved(self.display(token), token)

    def test_runtime_spell_must_match_verified_node_relation(self):
        self.scope('mismatched_runtime', 900030, 51271)
        self.node(900030, 910030, 1249658)
        self.assert_unresolved(self.display('mismatched_runtime'), 'mismatched_runtime')

    def test_unique_relation_without_runtime_spell_prefers_explicit_display_spell(self):
        self.scope('unique_relation', 900040)
        self.node(900040, 910040, 999040, display_spell_id=51271)
        self.snapshot(51271, name_zh='显示法术的中文')
        item = self.display('unique_relation')
        self.assertEqual(item['spell_id'], 51271)
        self.assertEqual(item['name'], '显示法术的中文')

    def test_runtime_can_match_node_spell_but_display_uses_display_spell(self):
        self.scope('node_spell_relation', 900050, 999050)
        self.node(900050, 910050, 999050, display_spell_id=51271)
        self.assertEqual(self.display('node_spell_relation')['spell_id'], 51271)

    def test_selected_version_and_canonical_compound_spec_are_isolated(self):
        self.scope('compound_spec', 900060, 51271, class_name='hunter', spec='beast_mastery')
        self.node(900060, 910060, 51271, class_name='Hunter', spec_name='BeastMastery', name_zh='正确专精')
        self.node(900060, 910061, 51271, class_name='Hunter', spec_name='Marksmanship', name_zh='错误专精')
        self.node(900060, 910062, 51271, class_name='Mage', spec_name='Frost', name_zh='错误职业')
        self.node(900060, 910063, 51271, class_name='Hunter', spec_name='BeastMastery',
                  talent_version=self.ptr, name_zh='PTR天赋')
        item = self.display('compound_spec', class_name='hunter', spec='beast_mastery')
        self.assertEqual(item['name'], '正确专精')
        item = self.display('compound_spec', version=self.ptr, class_name='hunter', spec='beast_mastery')
        self.assertEqual(item['name'], 'PTR天赋')

    def test_missing_selected_version_relation_does_not_borrow_other_version(self):
        self.scope('retail_only', 900070, 51271)
        self.node(900070, 910070, 51271)
        self.assert_unresolved(self.display('retail_only', version=self.ptr), 'retail_only')

    def test_effectless_anchor_and_entry_placeholder_never_become_spell_ids(self):
        for index, tree in enumerate(('hero_anchor', 'spec')):
            with self.subTest(tree=tree):
                token, trait, entry = f'placeholder_{index}', 900080 + index, 910080 + index
                self.scope(token, trait, entry)
                self.node(trait, entry, entry, display_spell_id=None, tree_type=tree,
                          description_zh='$@spelldesc51271', db2_subtree_id=60)
                self.snapshot(entry, name_zh='同号法术陷阱', description='错误说明')
                item = self.display(token)
                self.assertIsNone(item['spell_id'])
                self.assertNotIn('同号法术陷阱', item['name'])
                self.assertNotIn('$', item['description'])

    def test_unique_effectless_anchor_keeps_metadata_without_inventing_spell(self):
        self.scope('effectless_anchor', 900085)
        self.node(900085, 910085, 910085, display_spell_id=None,
                  tree_type='hero_anchor', db2_subtree_id=60,
                  name_zh='英雄天赋入口', description_zh='$@spelldesc51271')
        item = self.display('effectless_anchor')
        self.assertIsNone(item['spell_id'])
        self.assertEqual(item['name'], '英雄天赋入口')
        self.assertTrue(item['icon'])
        self.assertEqual(item['hero_tree_ids'], [60])
        self.assertEqual(item['description'], '当前资料尚未提供说明。')

    def test_plain_raw_description_survives_but_templates_never_leak(self):
        for index, (raw_zh, raw_en, expected) in enumerate((
                ('$s1 点伤害', 'Safe raw fallback.', 'Safe raw fallback.'),
                ('$@spelldesc51271', '?s51271[yes][no]', '当前资料尚未提供说明。'),
                ('普通中文说明。', '$s1', '普通中文说明。'))):
            with self.subTest(raw_zh=raw_zh):
                token, trait, entry = f'raw_text_{index}', 900090 + index, 910090 + index
                self.scope(token, trait)
                self.node(trait, entry, 999090 + index, description_zh=raw_zh, description=raw_en)
                item = self.display(token)
                self.assertEqual(item['description'], expected)
                self.assertNotIn('$', item['description'])
