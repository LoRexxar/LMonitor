import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase

from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTask


DEFAULT_GEAR = '''head=,id=212048,ilevel=639
neck=,id=212049,ilevel=639
shoulders=,id=212050,ilevel=639
back=,id=212051,ilevel=639
chest=,id=212052,ilevel=639
wrists=,id=212053,ilevel=639
hands=,id=212054,ilevel=639
waist=,id=212055,ilevel=639
legs=,id=212056,ilevel=639
feet=,id=212057,ilevel=639
finger1=,id=212058,ilevel=639
finger2=,id=212059,ilevel=639
trinket1=,id=212060,ilevel=639
trinket2=,id=212061,ilevel=639
main_hand=,id=222222,ilevel=639'''

DEFAULT_PLAYER = '''warrior="Upstream Fury"
level=90
race=orc
spec=fury
talents=UPSTREAM_BUILD
flask=flask_of_alchemical_chaos
''' + DEFAULT_GEAR + '\n'

BASE_TEMPLATE = (
    '{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n'
    '{stat_overrides}\n{action_list}\n{output_options}'
)
APL_CONTENT = 'actions=/auto_attack\nactions+=/bloodthirst'


class ImportSimcPlayerTemplatesTests(TestCase):
    def test_imports_only_base_mid1_profiles_and_sanitizes_executable_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / 'MID1_Warrior_Fury.simc').write_text(
                DEFAULT_PLAYER
                + 'actions=auto_attack\niterations=10000\n# Gear Summary\n# gear_ilvl=639\n',
                encoding='utf-8',
            )
            (source / 'MID1_Warrior_Fury_Slayer.simc').write_text(
                DEFAULT_PLAYER.replace('Upstream Fury', 'Hero Override'), encoding='utf-8'
            )
            (source / 'MID1_Hunter_Beast_Mastery.simc').write_text(
                DEFAULT_PLAYER.replace('warrior=', 'hunter=').replace('spec=fury', 'spec=beast_mastery'),
                encoding='utf-8',
            )

            call_command('import_simc_player_templates', source_dir=tmp, sync_version='abc123')

        rows = SimcContentTemplate.objects.filter(template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER)
        self.assertEqual(rows.count(), 2)
        fury = rows.get(spec='warrior_fury')
        self.assertEqual(fury.sync_version, 'abc123')
        self.assertIn('warrior="Upstream Fury"', fury.content)
        for forbidden in ('actions=', 'iterations=', 'Gear Summary', 'Hero Override'):
            self.assertNotIn(forbidden, fury.content)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(DEFAULT_PLAYER, encoding='utf-8')
            out = StringIO()
            call_command('import_simc_player_templates', source_dir=tmp, sync_version='abc123', dry_run=True, stdout=out)
        self.assertFalse(SimcContentTemplate.objects.exists())
        self.assertIn('DRY', out.getvalue())

    def test_rejects_profile_whose_actor_or_spec_does_not_match_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(
                DEFAULT_PLAYER.replace('warrior=', 'mage=').replace('spec=fury', 'spec=fire'),
                encoding='utf-8',
            )
            call_command('import_simc_player_templates', source_dir=tmp)
        self.assertFalse(SimcContentTemplate.objects.exists())

    def test_rejects_default_profile_below_level_90_or_with_incomplete_combat_gear(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            source.joinpath('MID1_Warrior_Fury.simc').write_text(
                DEFAULT_PLAYER.replace('level=90', 'level=80'), encoding='utf-8',
            )
            source.joinpath('MID1_Mage_Fire.simc').write_text(
                'mage="Incomplete"\nlevel=90\nspec=fire\nhead=,id=1\nmain_hand=,id=2\n',
                encoding='utf-8',
            )
            call_command('import_simc_player_templates', source_dir=tmp)
        self.assertFalse(SimcContentTemplate.objects.exists())

    def test_rejects_non_integer_or_future_level(self):
        for level in ('90.9', '91'):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'MID1_Warrior_Fury.simc').write_text(
                    DEFAULT_PLAYER.replace('level=90', f'level={level}'), encoding='utf-8',
                )
                call_command('import_simc_player_templates', source_dir=tmp)
                self.assertFalse(SimcContentTemplate.objects.exists())


class DefaultPlayerReferenceContractTests(TestCase):
    """Default-player imports remain source material; Tasks only use explicit resources."""

    def setUp(self):
        self.user = User.objects.create_user(username='default_player_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.default_player = self.add_default_player()
        # Edited temporary text is persisted as selectable resources before Task creation.
        self.template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            owner_user_id=self.user.id,
            spec='warrior_fury', name='Saved base template', content=BASE_TEMPLATE,
            is_active=True, is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
            spec='warrior_fury', name='Saved APL', content=APL_CONTENT,
            is_active=True, is_selectable=True,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Fury explicit profile',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment=DEFAULT_PLAYER,
            talent='USER_BUILD',
            is_active=True,
        )

    def add_default_player(self, content=DEFAULT_PLAYER):
        return SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', class_name='warrior', name='MID1 Fury player',
            content=content, sync_version='v1', is_active=True, is_selectable=False,
        )

    def task_payload(self, **overrides):
        payload = {
            'name': 'Fury reference task',
            'simc_profile_id': self.profile.id,
            'task_type': 1,
            'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id,
        }
        payload.update(overrides)
        return payload

    def test_task_references_saved_resources_and_immutable_versions(self):
        response = self.client.post(
            '/api/simc-task/', data=json.dumps(self.task_payload()), content_type='application/json'
        )
        self.assertTrue(response.json()['success'], response.json())

        task = SimcTask.objects.select_related(
            'profile_version', 'template_version', 'apl_version'
        ).get(id=response.json()['data']['id'])
        self.assertEqual(task.profile.name, 'Fury explicit profile')
        self.assertEqual(task.template_id, self.template.id)
        self.assertEqual(task.apl_id, self.apl.id)
        self.assertEqual(task.profile_version.resource_id, task.profile_id)
        self.assertEqual(task.template_version.resource_id, self.template.id)
        self.assertEqual(task.apl_version.resource_id, self.apl.id)
        self.assertEqual(task.profile_version.payload['player_equipment'].strip(), DEFAULT_PLAYER.strip())
        self.assertEqual(task.template_version.payload['content'], BASE_TEMPLATE)
        self.assertEqual(task.apl_version.payload['content'], APL_CONTENT)
        self.assertNotIn('player_equipment', json.loads(task.ext or '{}'))
        self.assertNotIn('base_template_content', json.loads(task.ext or '{}'))
        self.assertNotIn('override_action_list', json.loads(task.ext or '{}'))

    def test_default_player_is_not_implicitly_selected_for_task(self):
        self.profile.player_equipment = ''
        self.profile.save(update_fields=['player_equipment'])
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps(self.task_payload()),
            content_type='application/json',
        )
        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.select_related('profile_version').get(id=response.json()['data']['id'])
        self.assertEqual(task.profile_version.payload['player_equipment'], '')
        self.assertNotIn('Upstream Fury', json.dumps(task.profile_version.payload))

    def test_task_rejects_temporary_template_and_apl_bodies(self):
        for field, value, error_fragment in (
            ('base_template_content', BASE_TEMPLATE, 'base_template_content'),
            ('override_action_list', APL_CONTENT, 'override_action_list'),
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    '/api/simc-task/',
                    data=json.dumps(self.task_payload(**{field: value})),
                    content_type='application/json',
                )
                self.assertFalse(response.json()['success'])
                self.assertIn(error_fragment, response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())

    def test_task_requires_existing_profile_reference(self):
        payload = self.task_payload()
        payload.pop('simc_profile_id')
        response = self.client.post(
            '/api/simc-task/', data=json.dumps(payload), content_type='application/json'
        )
        self.assertFalse(response.json()['success'])
        self.assertIn('simc_profile_id', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())
        self.assertEqual(SimcProfile.objects.filter(user_id=self.user.id).count(), 1)

    def test_duplicate_active_default_templates_are_rejected_by_unique_constraint(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.add_default_player()
        self.assertEqual(
            SimcContentTemplate.objects.filter(
                template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER
            ).count(),
            1,
        )

    def test_attribute_detail_can_read_default_without_returning_raw_template_field(self):
        response = self.client.post('/api/simc-player-config-detail/', data=json.dumps({
            'spec': 'fury', 'player_config_mode': 'attribute_only', 'talent': 'USER_BUILD',
            'gear_crit': 1000, 'gear_haste': 2000, 'gear_mastery': 3000, 'gear_versatility': 4000,
        }), content_type='application/json')
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['identity']['name'], 'Upstream Fury')
        self.assertNotIn('player_equipment', payload['data'])
        self.assertNotIn('template_content', payload['data'])
