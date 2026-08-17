import hashlib
import importlib
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings

from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTalentString, SimcTask


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
    def test_data_migration_normalizes_existing_upstream_profiles_only(self):
        upstream = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', name='12.1 PTR Fury',
            player_equipment=(
                'warrior="Fury"\nptr=1\nshoulders=249312,ilevel=334\n'
                'wrists=249315,ilevel=334\nmain_hand=268213,ilevel=334'
            ),
        )
        user_profile = SimcProfile.objects.create(
            user_id=123, source=SimcProfile.SOURCE_USER, name='User Profile',
            player_equipment='main_hand=268213,ilevel=334',
        )
        migration = importlib.import_module(
            'botend.migrations.0150_normalize_mid_profile_equipment'
        )

        from django.apps import apps
        migration.normalize_current_upstream_profiles(apps, None)

        upstream.refresh_from_db()
        user_profile.refresh_from_db()
        self.assertNotIn('\nptr=', f'\n{upstream.player_equipment}')
        self.assertIn('shoulders=,id=249312,ilevel=334', upstream.player_equipment)
        self.assertIn('wrists=,id=249315,ilevel=334', upstream.player_equipment)
        self.assertIn('main_hand=,id=268213,ilevel=334', upstream.player_equipment)
        self.assertEqual(user_profile.player_equipment, 'main_hand=268213,ilevel=334')

    @patch(
        'botend.management.commands.import_simc_player_templates.REQUIRED_PROFILE_SPECS',
        {('warrior', 'fury')},
    )
    def test_import_normalizes_numeric_item_shorthand_and_drops_embedded_ptr(self):
        SimcProfile.objects.create(
            user_id=None,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            name='Stale upstream Fury',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            gear_strength=93330,
            gear_crit=10730,
            gear_haste=18641,
            gear_mastery=21785,
            gear_versatility=6757,
        )
        upstream = DEFAULT_PLAYER.replace(
            'head=,id=212048,ilevel=639',
            'head=212048,ilevel=639',
        ).replace(
            'main_hand=,id=222222,ilevel=639',
            'main_hand=222222,ilevel=639',
        ).replace('level=90', 'ptr=1\nlevel=90')
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(upstream, encoding='utf-8')
            call_command('import_simc_player_templates', source_dir=tmp, use_ptr=True)

        profile = SimcProfile.objects.get(system_key='simc_upstream:warrior_fury')
        talent = SimcTalentString.objects.get(system_key='simc_upstream:warrior_fury')
        self.assertIn('head=,id=212048,ilevel=639', profile.player_equipment)
        self.assertIn('main_hand=,id=222222,ilevel=639', profile.player_equipment)
        self.assertNotIn('\nptr=', f'\n{profile.player_equipment}')
        self.assertIs(profile.use_ptr, True)
        self.assertEqual(profile.talent, '')
        self.assertNotIn('talents=', profile.player_equipment)
        self.assertEqual(talent.system_key, profile.system_key)
        self.assertEqual(talent.spec, 'warrior_fury')
        self.assertEqual(talent.talent, 'UPSTREAM_BUILD')
        self.assertTrue(talent.is_system)
        stable_talent_id = talent.id
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(upstream, encoding='utf-8')
            call_command('import_simc_player_templates', source_dir=tmp, use_ptr=True)
        self.assertEqual(
            SimcTalentString.objects.get(system_key='simc_upstream:warrior_fury').id,
            stable_talent_id,
        )
        for field in ('gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility'):
            self.assertIsNone(getattr(profile, field), field)

    @patch(
        'botend.management.commands.import_simc_player_templates.REQUIRED_PROFILE_SPECS',
        {('warrior', 'fury')},
    )
    def test_import_rejects_system_talent_key_owned_by_user_without_mutating_it(self):
        collision = SimcTalentString.objects.create(
            system_key='simc_upstream:warrior_fury',
            owner_user_id=987,
            is_system=False,
            name='User-owned collision',
            spec='warrior_fury',
            talent='DO_NOT_OVERWRITE',
            is_active=True,
            is_selectable=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(DEFAULT_PLAYER, encoding='utf-8')
            with self.assertRaisesMessage(CommandError, '系统天赋键已被非系统资源占用'):
                call_command('import_simc_player_templates', source_dir=tmp)

        collision.refresh_from_db()
        self.assertEqual(collision.owner_user_id, 987)
        self.assertFalse(collision.is_system)
        self.assertEqual(collision.talent, 'DO_NOT_OVERWRITE')
        self.assertFalse(SimcProfile.objects.exists())

    @patch(
        'botend.management.commands.import_simc_player_templates.REQUIRED_PROFILE_SPECS',
        {('warrior', 'fury')},
    )
    def test_mid2_import_replaces_old_mid1_profile_and_marks_version(self):
        old = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', name='MID1 默认玩家 warrior_fury',
            class_name='warrior', spec='warrior_fury', profile_set='MID1', version='12.0',
            player_config_mode='manual_equipment', player_equipment='old', is_active=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID2_Warrior_Fury.simc').write_text(DEFAULT_PLAYER, encoding='utf-8')
            call_command(
                'import_simc_player_templates', source_dir=tmp, profile_set='MID2',
                profile_version='12.1', sync_version='b' * 40,
            )
        profile = SimcProfile.objects.get(system_key='simc_upstream:warrior_fury')
        self.assertNotEqual(profile.id, old.id)
        self.assertEqual(profile.profile_set, 'MID2')
        self.assertEqual(profile.version, '12.1')
        self.assertEqual(profile.sync_version, 'b' * 40)
        old.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertIsNone(old.system_key)
        self.assertNotEqual(profile.id, old.id)

    def test_required_mid1_profiles_match_the_supported_32_spec_execution_scope(self):
        from botend.management.commands.import_simc_player_templates import REQUIRED_PROFILE_SPECS

        self.assertEqual(len(REQUIRED_PROFILE_SPECS), 32)
        for unsupported in {
            ('druid', 'restoration'), ('evoker', 'augmentation'),
            ('evoker', 'preservation'), ('monk', 'mistweaver'),
            ('paladin', 'holy'), ('priest', 'discipline'),
            ('priest', 'holy'), ('shaman', 'restoration'),
        }:
            self.assertNotIn(unsupported, REQUIRED_PROFILE_SPECS)

    @patch(
        'botend.management.commands.import_simc_player_templates.REQUIRED_PROFILE_SPECS',
        {('warrior', 'fury'), ('hunter', 'beast_mastery')},
    )
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

        rows = SimcProfile.objects.filter(
            user_id__isnull=True,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
        )
        self.assertEqual(rows.count(), 2)
        fury = rows.get(spec='warrior_fury')
        self.assertEqual(fury.sync_version, 'abc123')
        self.assertIn('warrior="Upstream Fury"', fury.player_equipment)
        for forbidden in ('actions=', 'iterations=', 'Gear Summary', 'Hero Override'):
            self.assertNotIn(forbidden, fury.player_equipment)

    @patch(
        'botend.management.commands.import_simc_player_templates.REQUIRED_PROFILE_SPECS',
        {('warrior', 'fury')},
    )
    def test_explicit_ptr_import_marks_profile_without_version_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(DEFAULT_PLAYER, encoding='utf-8')
            call_command(
                'import_simc_player_templates',
                source_dir=tmp,
                use_ptr=True,
            )

        profile = SimcProfile.objects.get(system_key='simc_upstream:warrior_fury')
        self.assertEqual(profile.version, '12.0')
        self.assertIs(profile.use_ptr, True)

    @patch(
        'botend.management.commands.import_simc_player_templates.REQUIRED_PROFILE_SPECS',
        {('warrior', 'fury')},
    )
    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(DEFAULT_PLAYER, encoding='utf-8')
            out = StringIO()
            call_command('import_simc_player_templates', source_dir=tmp, sync_version='abc123', dry_run=True, stdout=out)
        self.assertFalse(SimcProfile.objects.exists())
        self.assertIn('DRY', out.getvalue())

    def test_rejects_profile_whose_actor_or_spec_does_not_match_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'MID1_Warrior_Fury.simc').write_text(
                DEFAULT_PLAYER.replace('warrior=', 'mage=').replace('spec=fury', 'spec=fire'),
                encoding='utf-8',
            )
            with self.assertRaises(CommandError):
                call_command('import_simc_player_templates', source_dir=tmp)
        self.assertFalse(SimcProfile.objects.exists())

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
            with self.assertRaises(CommandError):
                call_command('import_simc_player_templates', source_dir=tmp)
        self.assertFalse(SimcProfile.objects.exists())

    def test_rejects_non_integer_or_future_level(self):
        for level in ('90.9', '91'):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'MID1_Warrior_Fury.simc').write_text(
                    DEFAULT_PLAYER.replace('level=90', f'level={level}'), encoding='utf-8',
                )
                with self.assertRaises(CommandError):
                    call_command('import_simc_player_templates', source_dir=tmp)
                self.assertFalse(SimcProfile.objects.exists())


@override_settings(SIMC_APL_CURRENT_IDENTITY=('a' * 40, '12.0.1.70000'))
class DefaultPlayerReferenceContractTests(TestCase):
    """Default-player imports remain source material; Tasks only use explicit resources."""

    def setUp(self):
        self.user = User.objects.create_user(username='default_player_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.default_player = self.add_default_player()
        # Edited temporary text is persisted as selectable resources before Task creation.
        self.template = SimcContentTemplate.objects.create(
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
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(APL_CONTENT.encode()).hexdigest(),
            validation_revision='a' * 40,
            validation_game_build='12.0.1.70000',
        )
        validation = {
            'valid': True,
            'content_hash': hashlib.sha256(APL_CONTENT.encode()).hexdigest(),
            'revision': 'a' * 40,
            'game_build': '12.0.1.70000',
            'diagnostics': [],
        }
        validator = patch(
            'botend.services.simc_task_service.validate_apl_for_profile',
            return_value=validation,
        )
        validator.start()
        self.addCleanup(validator.stop)
        identity = patch(
            'botend.services.simc_task_service.current_validation_identity',
            return_value=('a' * 40, '12.0.1.70000'),
        )
        identity.start()
        self.addCleanup(identity.stop)
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Fury explicit profile',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment=DEFAULT_PLAYER,
            talent='USER_BUILD',
            is_active=True,
        )
        self.talent = SimcTalentString.objects.create(
            owner_user_id=self.user.id,
            name='Fury independent talent',
            spec='warrior_fury',
            talent='USER_BUILD',
        )

    def add_default_player(self, content=DEFAULT_PLAYER):
        return SimcProfile.objects.create(
            user_id=None,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            spec='warrior_fury', class_name='warrior', name='MID1 Fury player',
            player_config_mode='manual_equipment', player_equipment=content,
            sync_version='v1', is_active=True,
        )

    def task_payload(self, **overrides):
        payload = {
            'name': 'Fury reference task',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id,
            'talent_string_id': self.talent.id,
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
        frozen_player = task.profile_version.payload['player_equipment'].strip()
        expected_player = '\n'.join(
            line for line in DEFAULT_PLAYER.strip().splitlines()
            if line.partition('=')[0].strip().lower() != 'talents'
        )
        self.assertEqual(frozen_player, expected_player)
        self.assertNotIn('\ntalents=', f'\n{frozen_player}')
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

    def test_duplicate_system_default_profiles_are_rejected_by_unique_constraint(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.add_default_player()
        self.assertEqual(
            SimcProfile.objects.filter(
                user_id__isnull=True,
                source=SimcProfile.SOURCE_SIMC_UPSTREAM,
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
