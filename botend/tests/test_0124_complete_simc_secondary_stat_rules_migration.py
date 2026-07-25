from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CompleteSimcSecondaryStatRulesMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0123_collapse_simc_batches_into_tasks')]
    migrate_to = [('botend', '0124_complete_simc_secondary_stat_rules')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        SecondaryRule = old_apps.get_model('botend', 'SimcSecondaryStatRule')
        MasteryCoefficient = old_apps.get_model('botend', 'SimcMasteryCoefficient')

        SecondaryRule.objects.filter(class_name='warrior').delete()
        MasteryCoefficient.objects.filter(spec__in=('fury', 'devourer')).delete()
        MasteryCoefficient.objects.create(spec='fury', mastery_coefficient=1.55)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def test_adds_missing_rules_without_overwriting_existing_values(self):
        SecondaryRule = self.apps.get_model('botend', 'SimcSecondaryStatRule')
        MasteryCoefficient = self.apps.get_model('botend', 'SimcMasteryCoefficient')

        warrior = SecondaryRule.objects.get(class_name='warrior')
        self.assertEqual(warrior.crit_per_percent, 46)
        self.assertEqual(warrior.haste_per_percent, 44)
        self.assertEqual(warrior.mastery_per_percent, 46)
        self.assertEqual(warrior.versatility_per_percent, 54)
        self.assertEqual(
            MasteryCoefficient.objects.get(spec='fury').mastery_coefficient,
            1.55,
        )
        self.assertEqual(
            MasteryCoefficient.objects.get(spec='devourer').mastery_coefficient,
            1.0,
        )
