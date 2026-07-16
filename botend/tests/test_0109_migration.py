"""
Test 0109 migration: UserAplStorage + SimcContentTemplate APL types -> SimcApl
"""
from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Migration0109TestCase(TransactionTestCase):
    """Test forward and backward migration for 0109."""

    migrate_from = [('botend', '0108_simc_batch_request_manifest_and_status')]
    migrate_to = [('botend', '0109_create_simc_apl_and_migrate_data')]

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.old_apps = self.executor.loader.project_state(self.migrate_from).apps

        # Create old test data in 0108 state
        UserAplStorage = self.old_apps.get_model('botend', 'UserAplStorage')
        SimcContentTemplate = self.old_apps.get_model('botend', 'SimcContentTemplate')

        # UserAplStorage in 0108 has NO spec field
        UserAplStorage.objects.create(
            user_id=1,
            title='Personal APL 1',
            apl_code='actions=/auto_attack',
            is_active=True,
        )
        UserAplStorage.objects.create(
            user_id=2,
            title='Personal APL 2',
            apl_code='actions=/berserker_rage',
            is_active=False,
        )

        # SimcContentTemplate default_apl
        SimcContentTemplate.objects.create(
            name='Fury Default',
            template_type='default_apl',
            spec='warrior_fury',
            class_name='warrior',
            content='actions=/rampage',
            source='simc_upstream',
            is_active=True,
            is_selectable=True,
        )

        # SimcContentTemplate custom_apl
        SimcContentTemplate.objects.create(
            name='Custom Fury',
            template_type='custom_apl',
            spec='warrior_fury',
            class_name='warrior',
            content='actions=/execute',
            source='user',
            owner_user_id=1,
            is_active=True,
            is_selectable=True,
        )

        # Non-APL template (should remain)
        SimcContentTemplate.objects.create(
            name='Base Template',
            template_type='base_template',
            spec='default',
            content='iterations=10000',
            is_active=True,
        )

    def test_migration_forward(self):
        """Test 0108 -> 0109: data migration and model deletion."""
        # Run forward migration. Recreate the executor because its loader still
        # reflects the migration set from before setUp migrated back to 0108.
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        new_apps = self.executor.loader.project_state(self.migrate_to).apps

        SimcApl = new_apps.get_model('botend', 'SimcApl')
        SimcContentTemplate = new_apps.get_model('botend', 'SimcContentTemplate')

        # Check SimcApl has 4 records (2 UserAplStorage + 1 default_apl + 1 custom_apl)
        self.assertEqual(SimcApl.objects.count(), 4)

        # Check UserAplStorage data migrated with spec='unknown'
        personal_apls = SimcApl.objects.filter(is_system=False, source='user').order_by('owner_user_id')
        self.assertEqual(personal_apls.count(), 3)  # 2 from UserAplStorage + 1 from custom_apl

        user1_apls = personal_apls.filter(owner_user_id=1)
        self.assertTrue(user1_apls.filter(name='Personal APL 1', spec='unknown', content='actions=/auto_attack').exists())
        self.assertTrue(user1_apls.filter(name='Custom Fury', spec='warrior_fury', content='actions=/execute').exists())

        user2_apls = personal_apls.filter(owner_user_id=2)
        self.assertEqual(user2_apls.count(), 1)
        self.assertEqual(user2_apls.first().name, 'Personal APL 2')
        self.assertEqual(user2_apls.first().spec, 'unknown')
        self.assertFalse(user2_apls.first().is_active)

        # Check default_apl migrated as system APL
        system_apls = SimcApl.objects.filter(is_system=True)
        self.assertEqual(system_apls.count(), 1)
        fury_default = system_apls.first()
        self.assertEqual(fury_default.name, 'Fury Default')
        self.assertEqual(fury_default.spec, 'warrior_fury')
        self.assertEqual(fury_default.content, 'actions=/rampage')
        self.assertEqual(fury_default.source, 'simc_upstream')

        # Check UserAplStorage model deleted
        with self.assertRaises(LookupError):
            new_apps.get_model('botend', 'UserAplStorage')

        # Check old APL types deleted from SimcContentTemplate
        self.assertEqual(SimcContentTemplate.objects.filter(template_type='default_apl').count(), 0)
        self.assertEqual(SimcContentTemplate.objects.filter(template_type='custom_apl').count(), 0)

        # Check base_template remained
        self.assertEqual(SimcContentTemplate.objects.filter(template_type='base_template').count(), 1)
        base = SimcContentTemplate.objects.get(template_type='base_template')
        self.assertEqual(base.name, 'Base Template')
        self.assertEqual(base.content, 'iterations=10000')
