from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import SimcApl, SimcContentTemplate


class SimcHomeCreationResourceContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='home-flow', password='pwd')
        self.client.force_login(self.user)

    def _apl(self, name='Default', **overrides):
        values = {
            'name': name, 'spec': 'warrior_fury', 'class_name': 'warrior',
            'content': 'actions=/bloodthirst', 'source': SimcApl.SOURCE_SIMC_UPSTREAM,
            'is_system': True, 'owner_user_id': None, 'is_active': True, 'is_selectable': True,
        }
        values.update(overrides)
        return SimcApl.objects.create(**values)

    def _template(self, name='Base', **overrides):
        values = {
            'name': name, 'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
            'source': SimcContentTemplate.SOURCE_SIMC_UPSTREAM, 'spec': 'warrior_fury',
            'class_name': 'warrior', 'content': '{player_config}\n{apl}\n',
            'is_active': True, 'is_selectable': True, 'owner_user_id': None,
        }
        values.update(overrides)
        return SimcContentTemplate.objects.create(**values)

    def test_candidates_mark_only_the_unique_system_default_and_resolve_template(self):
        default = self._apl()
        personal = self._apl(
            name='Personal', source=SimcApl.SOURCE_USER, is_system=False,
            owner_user_id=self.user.id,
        )
        template = self._template()
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['default_template_id'], template.id)
        by_id = {row['id']: row for row in payload['data']}
        self.assertTrue(by_id[default.id]['is_default'])
        self.assertFalse(by_id[personal.id]['is_default'])

    def test_candidates_fail_when_default_apl_is_missing(self):
        self._template()
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 409)
        self.assertIn('默认 APL', response.json()['error'])

    def test_candidates_fail_when_default_template_is_missing(self):
        self._apl()
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 409)
        self.assertIn('基础模板', response.json()['error'])

    def test_candidates_fail_when_multiple_matching_templates_exist(self):
        self._apl()
        self._template(spec='default')
        self._template(name='All', spec='all')
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 409)
        self.assertIn('多个', response.json()['error'])