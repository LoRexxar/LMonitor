import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.dashboard.api import inspect_raw_simc_code
from botend.models import SimcContentTemplate, SimcTask


class SimcRawInspectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='simc_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_inspect_raw_simc_code_detects_profile_and_default_apl(self):
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='hunter_beast_mastery',
            class_name='hunter',
            name='默认APL hunter_beast_mastery',
            content='actions+=/kill_command',
            is_active=True,
            is_selectable=True,
        )
        payload = inspect_raw_simc_code('''
hunter="Bloodmastêr"
level=80
race=orc
role=attack
spec=beast_mastery
''')

        self.assertEqual(payload['character_name'], 'Bloodmastêr')
        self.assertEqual(payload['class'], 'hunter')
        self.assertEqual(payload['spec'], 'beast_mastery')
        self.assertEqual(payload['spec_key'], 'hunter_beast_mastery')
        self.assertTrue(payload['default_apl_available'])
        self.assertEqual(payload['plans'][0]['id'], 'regular')
        self.assertTrue(payload['plans'][0]['enabled'])
        self.assertFalse(payload['plans'][1]['enabled'])

    def test_inspect_raw_endpoint_returns_plans(self):
        response = self.client.post(
            '/api/simc-profile/inspect-raw/',
            data=json.dumps({'raw_simc_code': 'warrior="Foo"\nspec=fury\n'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['data']['class'], 'warrior')
        self.assertEqual(payload['data']['spec'], 'fury')
        self.assertEqual(payload['data']['plans'][0]['task_type'], 1)

    def test_raw_simc_task_create_persists_raw_code_in_ext(self):
        raw_code = 'mage="Arcaneone"\nspec=arcane\n'
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Arcaneone arcane 常规模拟',
                'task_type': 1,
                'simc_profile_id': 0,
                'raw_simc_code': raw_code,
                'regular_time': 300,
                'regular_target_count': 1,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.simc_profile_id, 0)
        self.assertEqual(task.task_type, 1)
        ext = json.loads(task.ext)
        self.assertEqual(ext['raw_simc_code'], raw_code)
        self.assertEqual(ext['regular_time'], 300)
        self.assertEqual(ext['regular_target_count'], 1)

    def test_raw_simc_attribute_task_is_rejected(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'bad attribute raw',
                'task_type': 2,
                'simc_profile_id': 0,
                'raw_simc_code': 'paladin="Foo"\nspec=retribution\n',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('不支持属性模拟', payload['error'])
        self.assertFalse(SimcTask.objects.exists())
