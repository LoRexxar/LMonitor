from django.test import SimpleTestCase

from LMonitor.config import DedicatedSimcWorkerSlot, Monitor_Type_BaseObject_List
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor


class SimcWorkerIntegrationTests(SimpleTestCase):
    def test_public_monitor_keeps_type_indexes_but_does_not_register_simc_consumer(self):
        self.assertNotIn(SimcMonitor, Monitor_Type_BaseObject_List)
        self.assertIs(Monitor_Type_BaseObject_List[15], DedicatedSimcWorkerSlot)
        self.assertTrue(DedicatedSimcWorkerSlot(None, None).scan())

    def test_frozen_canonical_spec_is_converted_at_worker_composer_boundary(self):
        from botend.controller.plugins.simc.SimcMonitor import _composer_identity

        self.assertEqual(_composer_identity('warrior_arms'), ('arms', 'warrior'))
        self.assertEqual(_composer_identity('fury'), ('fury', 'warrior'))
        self.assertEqual(_composer_identity('paladin_protection'), ('protection', 'paladin'))

    def test_deploy_only_runs_repeatable_release_steps(self):
        with open('deploy.sh', 'r', encoding='utf-8') as handle:
            script = handle.read()
        self.assertIn("git pull origin master", script)
        self.assertIn("manage.py migrate --no-input", script)
        self.assertIn("collectstatic --no-input --ignore='simc_results/*'", script)
        self.assertIn("manage.py runserver 0.0.0.0:18000 --noreload", script)
        self.assertIn("for session in lmweb lmback lmsimc", script)
        self.assertIn('curl -fsS http://127.0.0.1:18000/', script)
        self.assertIn("screen -S lmsimc -X quit", script)
        self.assertIn("screen -dmS lmsimc", script)
        self.assertIn("manage.py simc_worker", script)
        self.assertIn("lmweb|lmback|lmsimc", script)
        self.assertIn("flock -n 9", script)
        self.assertNotIn("manage.py update_simc_binary", script)
        self.assertNotIn("repair_ptr_talent_metadata", script)
        self.assertNotIn("import_mythic_dungeon_data", script)
        self.assertGreater(
            script.index("screen -S lmsimc -X quit"),
            script.index("=== 6. 重启 lmsimc ==="),
        )
