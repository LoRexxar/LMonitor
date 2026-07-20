from django.test import TestCase

from botend.models import (
    SimcAplSymbol, WowSpellSnapshot, WowSpecSpellMapSnapshot, WowTalentVersion,
    WowTalentNodeMetadata,
)
from botend.services.simc_apl.catalog import query_symbol_catalog


class SimcAplSymbolCatalogTests(TestCase):
    def symbol(self, **overrides):
        values = dict(simc_revision='r1', wow_build='b1', token='execute',
                      symbol_kind='action', source='system_apl')
        values.update(overrides)
        return SimcAplSymbol.objects.create(**values)

    def test_scope_merge_specificity_revision_isolation_and_kind_identity(self):
        self.symbol(token='shared', symbol_kind='action')
        self.symbol(token='shared', symbol_kind='namespace')
        self.symbol(token='shared', class_name='warrior', spell_id=1)
        self.symbol(token='shared', class_name='warrior', spec='fury', spell_id=2)
        self.symbol(token='other_spec', class_name='warrior', spec='arms')
        self.symbol(token='other_revision', simc_revision='r2')
        self.symbol(token='inactive', is_active=False)
        rows = query_symbol_catalog('r1', 'b1', 'warrior', 'fury')
        identities = {(row.token, row.kind): row for row in rows}
        self.assertEqual(identities[('shared', 'action')].spell_id, 2)
        self.assertIn(('shared', 'namespace'), identities)
        self.assertNotIn(('other_spec', 'action'), identities)
        self.assertNotIn(('other_revision', 'action'), identities)
        self.assertNotIn(('inactive', 'action'), identities)

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
