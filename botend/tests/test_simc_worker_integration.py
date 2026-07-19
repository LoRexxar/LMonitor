from django.test import SimpleTestCase

from LMonitor.config import DedicatedSimcWorkerSlot, Monitor_Type_BaseObject_List
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor


class SimcWorkerIntegrationTests(SimpleTestCase):
    def test_public_monitor_keeps_type_indexes_but_does_not_register_simc_consumer(self):
        self.assertNotIn(SimcMonitor, Monitor_Type_BaseObject_List)
        self.assertIs(Monitor_Type_BaseObject_List[15], DedicatedSimcWorkerSlot)
        self.assertTrue(DedicatedSimcWorkerSlot(None, None).scan())

    def test_deploy_manages_dedicated_lmsimc_screen(self):
        with open('deploy.sh', 'r', encoding='utf-8') as handle:
            script = handle.read()
        self.assertIn("screen -S lmsimc -X quit", script)
        self.assertIn("screen -dmS lmsimc", script)
        self.assertIn("manage.py simc_worker", script)
        self.assertIn("lmweb|lmback|lmsimc", script)
        self.assertIn("manage.py update_simc_binary --apply-patches", script)
        self.assertIn("flock -n 9", script)
        self.assertLess(
            script.index("screen -S lmsimc -X quit"),
            script.index("manage.py update_simc_binary --apply-patches"),
        )
