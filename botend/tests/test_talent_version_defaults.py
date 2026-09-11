import importlib
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.wow.talents.default_versions import ensure_default_talent_versions
from botend.wow.talents.versioning import TalentVersionResolver


class TalentVersionDefaultsTests(TestCase):
    def test_bootstrap_uses_stable_branch_keys_with_independent_versions(self):
        WowTalentVersion.objects.all().delete()
        legacy = WowTalentVersion.objects.create(
            key='ptr-12.1.0', label='正式服 12.1.0', branch='retail',
            major_version='12.1.0', is_active=True,
        )

        ensure_default_talent_versions(WowTalentVersion)
        ensure_default_talent_versions(WowTalentVersion)

        retail = WowTalentVersion.objects.get(key='retail')
        ptr = WowTalentVersion.objects.get(key='ptr')

        self.assertEqual(retail.pk, legacy.pk)
        self.assertEqual(retail.label, '正式服 12.1.0')
        self.assertEqual(retail.branch, 'retail')
        self.assertEqual(retail.major_version, '12.1.0')
        self.assertTrue(retail.is_active)
        self.assertTrue(retail.is_default_simulator)
        self.assertTrue(retail.is_default_player_tree)
        self.assertTrue(retail.is_default_stats)
        self.assertEqual(retail.status, 'active')

        self.assertEqual(ptr.label, '测试服 12.1.5（待同步）')
        self.assertEqual(ptr.branch, 'ptr')
        self.assertEqual(ptr.major_version, '12.1.5')
        self.assertFalse(ptr.is_active)
        self.assertFalse(ptr.is_default_simulator)
        self.assertFalse(ptr.is_default_player_tree)
        self.assertFalse(ptr.is_default_stats)

        self.assertFalse(WowTalentVersion.objects.filter(key='ptr-12.1.0').exists())
        self.assertEqual(
            TalentVersionResolver.get_default(TalentVersionResolver.USAGE_SIMULATOR),
            retail,
        )
        self.assertEqual(
            {version.key for version in TalentVersionResolver.list_active()},
            {'retail'},
        )

    def test_data_migration_preserves_current_tree_and_rehomes_live_historical_refs(self):
        WowTalentVersion.objects.all().delete()
        old = WowTalentVersion.objects.create(
            key='retail-12.0.7', label='正式服 12.0.7', branch='retail',
            major_version='12.0.7', is_active=False,
            is_default_player_tree=True,
        )
        current = WowTalentVersion.objects.create(
            key='ptr-12.1.0', label='正式服 12.1.0', branch='retail',
            major_version='12.1.0', current_build='12.1.0.69283', is_active=True,
        )
        current_node = WowTalentNodeMetadata.objects.create(
            talent_version=current, class_name='Mage', spec_name='Arcane',
            tree_type='spec', node_id=999001, spell_id=999002, name='Current',
        )
        for identity, name, name_zh in (
            (102435, 'Focused Enmity', '专注敌意'),
            (102436, 'Sanctuary', '庇护'),
        ):
            WowTalentNodeMetadata.objects.create(
                talent_version=old, class_name='Paladin', spec_name='Protection',
                tree_type='spec', node_id=identity, spell_id=identity + 1,
                display_spell_id=identity + 1, name=name, name_zh=name_zh,
                description_zh='历史说明', icon='historical_icon',
            )
        WowTalentNodeMetadata.objects.create(
            talent_version=old, class_name='Warrior', spec_name='Arms',
            tree_type='spec', node_id=888001, spell_id=888002, name='Obsolete',
        )

        migration = importlib.import_module('botend.migrations.0222_normalize_talent_versions')
        migration.normalize_talent_versions(
            apps, SimpleNamespace(connection=connection),
        )

        retail = WowTalentVersion.objects.get(key='retail')
        current_node.refresh_from_db()
        self.assertEqual(retail.pk, current.pk)
        self.assertEqual(current_node.talent_version_id, retail.pk)
        self.assertFalse(WowTalentVersion.objects.filter(key='retail-12.0.7').exists())
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(node_id=888001).exists())
        historical = WowTalentNodeMetadata.all_objects.filter(
            localization_only=True, name_kind='talent',
            reference_id__in=[102435, 102436],
        )
        self.assertEqual(historical.count(), 2)
        self.assertFalse(historical.exclude(class_name='', spec_name='').exists())
        ptr = WowTalentVersion.objects.get(key='ptr')
        self.assertEqual(ptr.major_version, '12.1.5')
        self.assertFalse(ptr.is_active)
