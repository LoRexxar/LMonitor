"""Contracts for staff-only Benchmark configuration option discovery."""
import json

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkPanel, SimcContentTemplate,
    SimcProfile,
)
from botend.services.simc_benchmark_config import (
    MAX_PROFILES_PER_SPEC, MAX_SCENARIOS, MAX_SPECS,
)


class SimcBenchmarkOptionsApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('option-owner', is_staff=True)
        self.editor = User.objects.create_user('option-editor', is_staff=True)
        self.other = User.objects.create_user('option-other')
        self.client.force_login(self.owner)

        self.backend = SimcBackendBinary.objects.create(
            identifier='ptr', name='PTR', simc_path='/secret/simc', is_active=True,
        )
        self.inactive_backend = SimcBackendBinary.objects.create(
            identifier='inactive', name='Inactive', is_active=False,
        )
        self.template = SimcContentTemplate.objects.create(
            name='Owner template', spec='warrior_fury', class_name='warrior',
            content='SECRET_TEMPLATE_BODY', owner_user_id=self.owner.id,
            is_active=True, is_selectable=True,
        )
        self.system_template = SimcContentTemplate.objects.create(
            name='System template', spec='default', content='SYSTEM_SECRET',
            owner_user_id=None, source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            is_active=True, is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='Owner APL', spec='warrior_fury', class_name='warrior',
            content='SECRET_APL_BODY', owner_user_id=self.owner.id,
            is_active=True, is_selectable=True,
        )
        self.system_apl = SimcApl.objects.create(
            name='System APL', spec='warrior_fury', class_name='warrior',
            content='SYSTEM_APL_SECRET', owner_user_id=None, is_system=True,
            source=SimcApl.SOURCE_SIMC_UPSTREAM, is_active=True, is_selectable=True,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.owner.id, name='Owner profile', spec='warrior_fury',
            class_name='warrior', battlenet_character='SECRET_CHARACTER',
            player_equipment='SECRET_EQUIPMENT', is_active=True,
        )
        self.default_profile = SimcProfile.objects.create(
            user_id=None, name='System profile', spec='warrior_fury',
            class_name='warrior', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            is_active=True,
        )

    def test_regular_simulation_raid_buff_catalog_is_available_to_authenticated_users(self):
        self.client.force_login(self.other)
        response = self.client.get('/api/simc-raid-buffs/options/')
        self.assertEqual(response.status_code, 200)
        rows = response.json()['data']
        self.assertEqual([row['value'] for row in rows], [
            'arcane_intellect', 'battle_shout', 'mark_of_the_wild',
            'power_word_fortitude', 'skyfury', 'chaos_brand', 'mystic_touch',
            'hunters_mark', 'mortal_wounds', 'bleeding', 'bloodlust',
        ])
        self.assertEqual(rows[0], {
            'value': 'arcane_intellect', 'label': '奥术智慧',
            'simc_option': 'override.arcane_intellect',
        })

    def test_specs_are_exact_supported_catalog_and_devourer_is_localized(self):
        response = self.client.get('/api/simc-benchmarks/options/')
        self.assertEqual(response.status_code, 200)
        specs = response.json()['data']['specs']
        self.assertEqual(len(specs), 40)
        self.assertEqual(len({row['value'] for row in specs}), 40)
        devourer = next(row for row in specs if row['value'] == 'demonhunter_devourer')
        self.assertEqual(devourer['spec_label'], '噬灭')
        self.assertEqual(devourer['role'], 'dps')
        self.assertEqual(set(devourer), {
            'value', 'spec_key', 'class_name', 'class_label', 'spec_label', 'label', 'role',
        })
        self.assertEqual({row['role'] for row in specs}, {'dps', 'tank', 'healer'})
        self.assertEqual(
            next(row for row in specs if row['value'] == 'deathknight_blood')['role'],
            'tank',
        )
        self.assertEqual(
            next(row for row in specs if row['value'] == 'priest_holy')['role'],
            'healer',
        )

    def test_options_publish_exact_fight_styles_accepted_by_current_simc_source(self):
        data = self.client.get('/api/simc-benchmarks/options/').json()['data']
        self.assertEqual([row['value'] for row in data['fight_styles']], [
            'Patchwerk', 'CastingPatchwerk', 'HecticAddCleave', 'DungeonSlice',
            'DungeonRoute', 'CleaveAdd', 'LightMovement', 'HeavyMovement',
            'beastlord', 'HelterSkelter', 'Ultraxion',
        ])
        self.assertEqual(data['fight_styles'][0]['label'], 'Patchwerk（木桩）')

    def test_options_publish_server_owned_raid_buff_catalog(self):
        data = self.client.get('/api/simc-benchmarks/options/').json()['data']
        self.assertEqual([row['value'] for row in data['raid_buffs']], [
            'arcane_intellect', 'battle_shout', 'mark_of_the_wild',
            'power_word_fortitude', 'skyfury', 'chaos_brand', 'mystic_touch',
            'hunters_mark', 'mortal_wounds', 'bleeding', 'bloodlust',
        ])
        self.assertEqual(data['raid_buffs'][0], {
            'value': 'arcane_intellect', 'label': '奥术智慧',
            'simc_option': 'override.arcane_intellect',
        })

    def test_create_and_edit_use_different_immutable_ownership_contexts(self):
        created = self.client.get('/api/simc-benchmarks/options/').json()['data']
        self.assertEqual(created['ownership_context'], 'current_user')
        self.assertIn(self.profile.id, [row['id'] for row in created['resources']['profiles']])
        panel = SimcBenchmarkPanel.objects.create(
            name='Panel', slug='option-panel', created_by_id=self.owner.id,
        )
        editor = Client()
        editor.force_login(self.editor)
        edited = editor.get(
            f'/api/simc-benchmarks/panels/{panel.id}/options/',
        ).json()['data']
        self.assertEqual(edited['ownership_context'], 'panel_creator')
        self.assertNotIn('owner_id', json.dumps(edited))
        self.assertIn(self.profile.id, [row['id'] for row in edited['resources']['profiles']])

    def test_filters_other_owner_inactive_and_nonselectable_but_keeps_system(self):
        other_template = SimcContentTemplate.objects.create(
            name='Other template', spec='mage_fire', content='x',
            owner_user_id=self.editor.id, is_active=True, is_selectable=True,
        )
        hidden_apl = SimcApl.objects.create(
            name='Hidden APL', spec='mage_fire', content='x',
            owner_user_id=self.owner.id, is_active=True, is_selectable=False,
        )
        other_profile = SimcProfile.objects.create(
            user_id=self.editor.id, name='Other profile', spec='mage_fire', is_active=True,
        )
        data = self.client.get('/api/simc-benchmarks/options/').json()['data']['resources']
        backend_ids = [row['id'] for row in data['backends']]
        self.assertIn(self.backend.id, backend_ids)
        self.assertNotIn(self.inactive_backend.id, backend_ids)
        self.assertNotIn(other_template.id, [row['id'] for row in data['templates']])
        self.assertNotIn(hidden_apl.id, [row['id'] for row in data['apls']])
        self.assertNotIn(other_profile.id, [row['id'] for row in data['profiles']])
        self.assertIn(self.system_template.id, [row['id'] for row in data['templates']])
        self.assertIn(self.system_apl.id, [row['id'] for row in data['apls']])
        self.assertIn(self.default_profile.id, [row['id'] for row in data['profiles']])

    def test_safe_projection_and_dynamic_limits(self):
        response = self.client.get('/api/simc-benchmarks/options/')
        body = response.content.decode()
        for secret in ('/secret/simc', 'SECRET_TEMPLATE_BODY', 'SECRET_APL_BODY',
                       'SECRET_CHARACTER', 'SECRET_EQUIPMENT'):
            self.assertNotIn(secret, body)
        self.assertEqual(response.json()['data']['limits'], {
            'max_specs': MAX_SPECS,
            'max_profiles_per_spec': MAX_PROFILES_PER_SPEC,
            'max_scenarios': MAX_SCENARIOS,
        })

    def test_create_defaults_are_authoritative_and_allow_multiple_active_profiles(self):
        response = self.client.get('/api/simc-benchmarks/options/')
        defaults = response.json()['data']['create_defaults']['warrior_fury']
        self.assertTrue(defaults['available'])
        production = SimcBackendBinary.objects.get(identifier='production')
        self.assertEqual(defaults['backend_id'], production.id)
        self.assertEqual(defaults['apl_id'], self.system_apl.id)
        self.assertEqual(defaults['template_id'], self.template.id)
        self.assertEqual(defaults['profile_id'], self.default_profile.id)

        later_profile = SimcProfile.objects.create(
            user_id=None, name='ZZZ later system profile', spec='warrior_fury',
            class_name='warrior', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            is_active=False,
        )
        SimcProfile.objects.filter(pk=later_profile.pk).update(
            is_active=True, system_key='simc_upstream:warrior_fury:12.1',
        )
        defaults = self.client.get('/api/simc-benchmarks/options/').json()['data'][
            'create_defaults'
        ]['warrior_fury']
        self.assertTrue(defaults['available'])
        self.assertEqual(defaults['profile_id'], self.default_profile.id)
        self.assertEqual(defaults['profile_label'], self.default_profile.name)

        self.default_profile.is_active = False
        self.default_profile.save(update_fields=['is_active'])
        defaults = self.client.get('/api/simc-benchmarks/options/').json()['data'][
            'create_defaults'
        ]['warrior_fury']
        self.assertTrue(defaults['available'])
        self.assertEqual(defaults['profile_id'], later_profile.id)

        SimcProfile.objects.filter(pk=later_profile.pk).update(is_active=False)
        defaults = self.client.get('/api/simc-benchmarks/options/').json()['data'][
            'create_defaults'
        ]['warrior_fury']
        self.assertFalse(defaults['available'])
        self.assertIn('Profile', defaults['reason'])
        self.assertNotIn('profile_id', defaults)

    def test_resources_expose_canonical_spec_keys_and_generic_template_key(self):
        """The browser must not guess whether a short or malformed spec is generic."""
        short_apl = SimcApl.objects.create(
            name='Short Fury APL', spec='fury', class_name='warrior', content='x',
            owner_user_id=self.owner.id, is_active=True, is_selectable=True,
        )
        short_profile = SimcProfile.objects.create(
            user_id=self.owner.id, name='Short Fury profile', spec='fury',
            class_name='warrior', is_active=True,
        )
        invalid_apl = SimcApl.objects.create(
            name='Invalid APL', spec='generic', content='x',
            owner_user_id=self.owner.id, is_active=True, is_selectable=True,
        )
        data = self.client.get('/api/simc-benchmarks/options/').json()['data']['resources']

        def by_id(kind, resource_id):
            return next(row for row in data[kind] if row['id'] == resource_id)

        self.assertEqual(by_id('templates', self.template.id)['spec_key'], 'warrior_fury')
        self.assertEqual(by_id('apls', self.apl.id)['spec_key'], 'warrior_fury')
        self.assertEqual(by_id('profiles', self.profile.id)['spec_key'], 'warrior_fury')
        self.assertEqual(by_id('apls', short_apl.id)['spec_key'], 'warrior_fury')
        self.assertEqual(by_id('profiles', short_profile.id)['spec_key'], 'warrior_fury')
        self.assertEqual(by_id('templates', self.system_template.id)['spec_key'], '')
        # Empty only means generic to the template picker. Invalid APL/profile rows
        # remain visible in options but cannot match a specialization in the UI.
        self.assertEqual(by_id('apls', invalid_apl.id)['spec_key'], '')

    def test_nonstaff_forbidden_missing_panel_not_found_and_methods_are_strict(self):
        regular = Client()
        regular.force_login(self.other)
        self.assertEqual(regular.get('/api/simc-benchmarks/options/').status_code, 403)
        self.assertEqual(self.client.get(
            '/api/simc-benchmarks/panels/999999/options/',
        ).status_code, 404)
        response = self.client.post(
            '/api/simc-benchmarks/options/', data='{}', content_type='application/json',
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()['error'], 'method_not_allowed')

    def test_query_count_is_bounded_by_resource_types_not_row_count(self):
        for index in range(5):
            SimcProfile.objects.create(
                user_id=self.owner.id, name=f'Profile {index}', spec='mage_fire',
            )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/simc-benchmarks/options/')
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 7)
