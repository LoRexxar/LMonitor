from django.test import TestCase

from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, WowSpellSnapshot, WowSpecSpellMapSnapshot, WowTalentVersion,
    WowTalentNodeMetadata,
)
from botend.services.simc_apl.catalog import query_symbol_catalog


class SimcAplSymbolCatalogTests(TestCase):
    def symbol(self, **overrides):
        values = dict(token='execute', symbol_kind='action', source='system_apl')
        values.update(overrides)
        values.pop('simc_revision', None)
        values.pop('wow_build', None)
        token = values.pop('token')
        kind = values.pop('symbol_kind')
        symbol, _created = SimcAplSymbol.objects.get_or_create(
            token=token, symbol_kind=kind,
        )
        return SimcAplSymbolScope.objects.create(symbol=symbol, **values)

    def test_scope_merge_specificity_and_kind_identity(self):
        self.symbol(token='shared', symbol_kind='action')
        self.symbol(token='shared', symbol_kind='namespace')
        self.symbol(token='shared', class_name='warrior', spell_id=1)
        self.symbol(token='shared', class_name='warrior', spec='fury', spell_id=2)
        self.symbol(token='other_spec', class_name='warrior', spec='arms')
        self.symbol(token='other_field', simc_revision='r2')
        self.symbol(token='inactive', is_active=False)
        rows = query_symbol_catalog('r1', 'b1', 'warrior', 'fury')
        identities = {(row.token, row.kind): row for row in rows}
        self.assertEqual(identities[('shared', 'action')].spell_id, 2)
        self.assertIn(('shared', 'namespace'), identities)
        self.assertNotIn(('other_spec', 'action'), identities)
        self.assertIn(('other_field', 'action'), identities)
        self.assertNotIn(('inactive', 'action'), identities)

    def test_unversioned_catalog_inherits_class_scope_across_all_class_specs(self):
        self.symbol(
            token='burst_of_power', symbol_kind='buff', class_name='warrior',
            spec=None, name_zh='能量爆发', simc_revision='old-revision',
            wow_build='old-build',
        )

        for spec in ('arms', 'fury', 'protection'):
            with self.subTest(spec=spec):
                rows = query_symbol_catalog(
                    None, None, 'warrior', spec, search='burst_of_power',
                )
                self.assertEqual([(row.token, row.name) for row in rows], [
                    ('burst_of_power', '能量爆发'),
                ])

    def test_one_token_kind_subject_can_hold_multiple_scope_bindings(self):
        warrior = self.symbol(
            token='shared_buff', symbol_kind='buff', class_name='warrior',
            name_zh='', simc_revision='old-revision', wow_build='old-build',
        )
        self.symbol(
            token='shared_buff', symbol_kind='buff', class_name='mage',
            name_zh='共享增益', simc_revision='new-revision', wow_build='new-build',
        )
        warrior.name_zh = '战士共享增益'
        warrior.save(update_fields=['name_zh'])

        rows = query_symbol_catalog(None, None, 'warrior', 'fury')
        shared = [row for row in rows if row.token == 'shared_buff']

        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0].name, '战士共享增益')
        self.assertEqual(SimcAplSymbol.objects.filter(
            token='shared_buff', symbol_kind='buff').count(), 1)
        self.assertEqual(SimcAplSymbolScope.objects.filter(
            symbol__token='shared_buff', symbol__symbol_kind='buff').count(), 2)

    def test_localization_fallback_search_and_bound_insertability(self):
        WowSpellSnapshot.objects.create(branch='wow', locale='enUS', spell_id=23881,
                                         name='Bloodthirst', snapshot_build='b1')
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=23881,
                                         name_zh='嗜血', description='说明', snapshot_build='b1')
        self.symbol(token='bloodthirst', class_name='warrior', spec='fury', spell_id=23881)
        row = query_symbol_catalog('r1', 'b1', 'warrior', 'fury', search='嗜血')[0]
        self.assertEqual((row.name, row.name_en), ('嗜血', 'Bloodthirst'))
        self.assertTrue(row.insertable)
        self.assertIsNone(row.reason)
        self.assertEqual(query_symbol_catalog('r1', 'b1', 'warrior', 'fury', search='23881')[0].token,
                         'bloodthirst')

    def test_packaged_localization_is_used_when_spell_snapshot_is_missing(self):
        self.symbol(
            token='bloodthirst', class_name='warrior', spec='fury', spell_id=23881,
            name_en='Bloodthirst', name_zh='嗜血',
        )
        row = query_symbol_catalog('r1', 'b1', 'warrior', 'fury', search='嗜血')[0]
        self.assertEqual((row.name, row.name_en), ('嗜血', 'Bloodthirst'))

    def test_source_supported_pet_and_future_fields_remain_visible_with_metadata(self):
        self.symbol(
            token='future_pet_buff', class_name='warrior', spec='fury',
            symbol_kind='buff', name_zh='未来宠物增益',
            metadata={
                'apl_expression_template': 'buff.future_pet_buff.up',
                'source_coverage': {
                    'availability': '12.1_mid2',
                    'actor': 'pet',
                    'insertable': False,
                    'insert_reason': '需要宠物表达式上下文',
                },
            },
        )
        row = query_symbol_catalog(
            'r1', 'b1', 'warrior', 'fury', search='future_pet_buff',
        )[0]
        self.assertEqual(row.name, '未来宠物增益')
        self.assertFalse(row.insertable)
        self.assertEqual(row.reason, '需要宠物表达式上下文')
        self.assertEqual(row.availability, '12.1_mid2')
        self.assertEqual(row.actor, 'pet')
        self.assertEqual(row.expression_template, 'buff.future_pet_buff.up')

    def test_unbound_talent_is_visible_but_never_guesses_token(self):
        version = WowTalentVersion.objects.create(
            key='b1', current_build='b1', is_active=True, is_default_simulator=True)
        WowTalentNodeMetadata.objects.create(class_name='Warrior', spec_name='Fury',
            tree_type='spec', node_id=1, spell_id=999, name='Imaginary Strike', name_zh='想象打击',
            icon='icon', talent_version=version)
        row = query_symbol_catalog('r1', 'b1', 'warrior', 'fury', search='想象')[0]
        self.assertIsNone(row.token)
        self.assertFalse(row.insertable)
        self.assertEqual(row.reason, '尚无 SimC APL token 映射')
        self.assertEqual(row.spell_id, 999)

    def test_wago_catalog_never_falls_back_across_builds(self):
        other = WowTalentVersion.objects.create(key='b2', current_build='b2')
        WowTalentNodeMetadata.objects.create(
            class_name='Warrior', spec_name='Fury', tree_type='spec', node_id=2,
            spell_id=998, name='Future Strike', talent_version=other,
        )
        WowSpecSpellMapSnapshot.objects.create(
            spec_id=72, spell_id=997, snapshot_build='b2',
        )
        rows = query_symbol_catalog('r1', 'b1', 'warrior', 'fury', spec_id=72)
        self.assertFalse({997, 998} & {row.spell_id for row in rows})

    def test_authoritative_spec_id_includes_unbound_spec_spell_map_item(self):
        WowSpecSpellMapSnapshot.objects.create(spec_id=72, spell_id=1234, snapshot_build='b1')
        WowSpellSnapshot.objects.create(locale='enUS', spell_id=1234, name='Mapped Spell',
                                         snapshot_build='b1')
        rows = query_symbol_catalog('r1', 'b1', 'warrior', 'fury', spec_id=72)
        row = next(item for item in rows if item.spell_id == 1234)
        self.assertIsNone(row.token)
        self.assertFalse(row.insertable)

    def test_talents_use_only_unique_active_default_version_for_build(self):
        historical = WowTalentVersion.objects.create(key='old-b1', current_build='b1')
        authoritative = WowTalentVersion.objects.create(
            key='current-b1', current_build='b1', is_active=True,
            is_default_simulator=True)
        for version, spell_id, name in ((historical, 901, 'Historical'),
                                        (authoritative, 902, 'Authoritative')):
            WowTalentNodeMetadata.objects.create(
                class_name='Warrior', spec_name='Fury', tree_type='spec',
                node_id=spell_id, spell_id=spell_id, name=name, talent_version=version)
        rows = query_symbol_catalog('r1', 'b1', 'warrior', 'fury')
        self.assertEqual({r.spell_id for r in rows if r.source == 'wago'}, {902})

    def test_multiple_authoritative_talent_versions_for_build_fail(self):
        WowTalentVersion.objects.create(key='one', current_build='b1', is_active=True,
                                        is_default_simulator=True)
        WowTalentVersion.objects.create(key='two', current_build='b1', is_active=True,
                                        is_default_simulator=True)
        with self.assertRaisesRegex(ValueError, 'talent version'):
            query_symbol_catalog('r1', 'b1', 'warrior', 'fury')
