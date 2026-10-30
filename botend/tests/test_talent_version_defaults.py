import importlib
from types import SimpleNamespace

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
            major_version='12.1.0', is_active=True, status='active',
            is_default_simulator=True, is_default_player_tree=True, is_default_stats=True,
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

        self.assertEqual(ptr.label, 'PTR')
        self.assertEqual(ptr.branch, 'ptr')
        self.assertEqual(ptr.major_version, '')
        self.assertFalse(ptr.is_active)
        self.assertFalse(ptr.is_default_simulator)
        self.assertFalse(ptr.is_default_player_tree)
        self.assertFalse(ptr.is_default_stats)

        self.assertFalse(WowTalentVersion.objects.filter(key='ptr-12.1.0').exists())
        self.assertEqual(TalentVersionResolver.resolve('ptr-12.1.0'), retail)
        self.assertEqual(TalentVersionResolver.resolve('retail-12.0.7'), retail)
        self.assertEqual(
            TalentVersionResolver.get_default(TalentVersionResolver.USAGE_SIMULATOR),
            retail,
        )
        self.assertEqual(
            {version.key for version in TalentVersionResolver.list_active()},
            {'retail'},
        )

    def test_empty_slots_stay_inactive_and_existing_provenance_never_rewinds(self):
        WowTalentVersion.objects.all().delete()
        ensure_default_talent_versions(WowTalentVersion)
        self.assertEqual(set(WowTalentVersion.objects.values_list('key', flat=True)), {'retail', 'ptr', 'beta'})
        self.assertEqual(TalentVersionResolver.list_active(), [])
        self.assertIsNone(TalentVersionResolver.resolve())
        retail = WowTalentVersion.objects.get(key='retail')
        retail.major_version, retail.source_dir = '14.2.0', '/real/source'
        retail.save()
        before = list(WowTalentVersion.objects.order_by('pk').values())
        ensure_default_talent_versions(WowTalentVersion)
        ensure_default_talent_versions(WowTalentVersion)
        self.assertEqual(list(WowTalentVersion.objects.order_by('pk').values()), before)

    def test_legacy_links_map_by_branch_and_never_resurrect_inactive_slot(self):
        ensure_default_talent_versions(WowTalentVersion)
        old = WowTalentVersion.objects.create(key='beta-13.0', branch='beta', is_active=True, status='active')
        beta = WowTalentVersion.objects.get(key='beta')
        with self.assertRaises(ValueError):
            TalentVersionResolver.resolve(old.key)
        beta.is_active, beta.status = True, 'active'
        beta.save()
        self.assertEqual(TalentVersionResolver.resolve(old.key), beta)
        self.assertNotIn(old, TalentVersionResolver.list_active())
        self.assertEqual(TalentVersionResolver.resolve(old.key, allow_inactive=True), old)

    def test_slot_migration_preserves_historical_rows_and_node_foreign_keys(self):
        from django.db.migrations.loader import MigrationLoader
        apps = MigrationLoader(connection).project_state([('botend', '0260_monitor_task_lease_is_manual')]).apps
        Version = apps.get_model('botend', 'WowTalentVersion')
        Version.objects.all().delete()
        old = Version.objects.create(key='beta-13.0', branch='beta', status='active', is_active=True,
                                     current_build='13.0.0.90000')
        node = WowTalentNodeMetadata.objects.create(talent_version_id=old.pk, node_id=987001)
        historical = Version.objects.create(key='beta-12.0', branch='beta', is_active=False)
        migration = importlib.import_module('botend.migrations.0261_talent_server_slots')
        for _ in range(2):
            migration.ensure_server_slots(apps, SimpleNamespace(connection=connection))
        self.assertEqual(Version.objects.get(key='beta').pk, old.pk)
        node.refresh_from_db()
        self.assertEqual(node.talent_version_id, old.pk)
        self.assertTrue(Version.objects.filter(pk=historical.pk).exists())
        self.assertEqual(set(Version.objects.filter(key__in=['retail', 'ptr', 'beta']).values_list('key', flat=True)), {'retail', 'ptr', 'beta'})

    def test_slot_migration_rehomes_populated_tree_without_deleting_empty_slot(self):
        from django.db.migrations.loader import MigrationLoader
        apps = MigrationLoader(connection).project_state([('botend', '0260_monitor_task_lease_is_manual')]).apps
        Version = apps.get_model('botend', 'WowTalentVersion')
        Version.objects.all().delete()
        empty = Version.objects.create(key='beta', branch='beta', is_active=False, status='draft')
        real = Version.objects.create(key='beta-13.0', branch='beta', is_active=True, status='active',
                                      current_build='13.0.0.90000')
        node = WowTalentNodeMetadata.objects.create(talent_version_id=real.pk, node_id=987002)
        migration = importlib.import_module('botend.migrations.0261_talent_server_slots')
        for _ in range(2):
            migration.ensure_server_slots(apps, SimpleNamespace(connection=connection))
        self.assertEqual(TalentVersionResolver.resolve('beta').pk, real.pk)
        self.assertEqual(TalentVersionResolver.resolve('beta-13.0').pk, real.pk)
        node.refresh_from_db()
        self.assertEqual(node.talent_version_id, real.pk)
        self.assertTrue(Version.objects.filter(pk=empty.pk, is_active=False).exists())

    def test_slot_migration_refuses_two_populated_active_trees_without_changes(self):
        from django.db.migrations.loader import MigrationLoader
        apps = MigrationLoader(connection).project_state([('botend', '0260_monitor_task_lease_is_manual')]).apps
        Version = apps.get_model('botend', 'WowTalentVersion')
        Version.objects.all().delete()
        for key, build, node_id in (('retail', '12.0.0.80000', 987003), ('retail-13.0', '13.0.0.90000', 987004)):
            row = Version.objects.create(key=key, branch='retail', is_active=True, status='active', current_build=build)
            WowTalentNodeMetadata.objects.create(talent_version_id=row.pk, node_id=node_id)
        before = list(Version.objects.order_by('pk').values())
        migration = importlib.import_module('botend.migrations.0261_talent_server_slots')
        with self.assertRaisesRegex(ValueError, 'Conflicting populated talent trees'):
            migration.ensure_server_slots(apps, SimpleNamespace(connection=connection))
        self.assertEqual(list(Version.objects.order_by('pk').values()), before)

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
        # 使用身份保护加入前、与测试表字段一致的迁移模型，避免调用当前运行时删除规则。
        from django.db.migrations.loader import MigrationLoader
        historical_apps = MigrationLoader(connection).project_state(
            [('botend', '0253_character_bindings')]).apps
        migration.normalize_talent_versions(
            historical_apps, SimpleNamespace(connection=connection),
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
