import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase

from botend.models import SimcContentTemplate, SimcProfile, SimcTask


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
EXPLICIT_PLAYER = DEFAULT_PLAYER.replace('Upstream Fury', 'Explicit').replace('id=212048', 'id=299001')


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
            source = Path(tmp)
            source.joinpath('MID1_Warrior_Fury.simc').write_text(
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


class DefaultPlayerTemplateAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='default_player_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def add_template(self, content=DEFAULT_PLAYER):
        return SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', class_name='warrior', name='MID1 Fury player',
            content=content, sync_version='v1', is_active=True, is_selectable=False,
        )

    def task_payload(self, **overrides):
        payload = {
            'name': 'Fury default baseline', 'task_type': 1, 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'USER_BUILD',
            'gear_strength': 5000, 'gear_crit': 1000, 'gear_haste': 2000,
            'gear_mastery': 3000, 'gear_versatility': 4000,
        }
        payload.update(overrides)
        return payload

    def test_profile_save_freezes_default_player_template(self):
        self.add_template()
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            **self.task_payload(), 'name': 'saved default',
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        profile = SimcProfile.objects.get(id=response.json()['data']['id'])
        self.assertEqual(profile.player_equipment, DEFAULT_PLAYER.strip())

    def test_regular_task_freezes_default_and_template_update_does_not_change_task(self):
        template = self.add_template()
        response = self.client.post('/api/simc-task/', data=json.dumps(self.task_payload()), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        frozen = json.loads(task.ext)['player_equipment']
        template.content = DEFAULT_PLAYER.replace('id=212048', 'id=999999')
        template.save(update_fields=['content'])
        task.refresh_from_db()
        self.assertEqual(json.loads(task.ext)['player_equipment'], frozen)
        self.assertIn('id=212048', frozen)

    def test_explicit_player_baseline_has_priority(self):
        self.add_template()
        response = self.client.post('/api/simc-task/', data=json.dumps(
            self.task_payload(player_equipment=EXPLICIT_PLAYER)
        ), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        ext = json.loads(SimcTask.objects.get(id=response.json()['data']['id']).ext)
        self.assertIn('warrior="Explicit"', ext['player_equipment'])
        self.assertIn('id=299001', ext['player_equipment'])

    def test_missing_default_template_returns_clear_error(self):
        response = self.client.post('/api/simc-task/', data=json.dumps(self.task_payload()), content_type='application/json')
        self.assertFalse(response.json()['success'])
        self.assertIn('默认玩家装备模板', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())

    def test_attribute_batch_freezes_default_into_every_task(self):
        self.add_template()
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            **self.task_payload(), 'kind': 'attribute_variants', 'attribute_step': 50,
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        task_ids = response.json()['data']['task_ids']
        frozen = {json.loads(row.ext)['player_equipment'] for row in SimcTask.objects.filter(id__in=task_ids)}
        self.assertEqual(frozen, {DEFAULT_PLAYER.strip()})

    def test_empty_attribute_detail_uses_default_without_returning_raw_template_field(self):
        self.add_template()
        response = self.client.post('/api/simc-player-config-detail/', data=json.dumps({
            'spec': 'fury', 'player_config_mode': 'attribute_only', 'talent': 'USER_BUILD',
            'gear_crit': 1000, 'gear_haste': 2000, 'gear_mastery': 3000, 'gear_versatility': 4000,
        }), content_type='application/json')
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['identity']['name'], 'Upstream Fury')
        self.assertNotIn('player_equipment', payload['data'])
        self.assertNotIn('template_content', payload['data'])

    def test_dashboard_death_knight_alias_resolves_exact_default_template(self):
        content = DEFAULT_PLAYER.replace('warrior=', 'deathknight=').replace('spec=fury', 'spec=frost')
        template = self.add_template(content)
        template.spec = 'deathknight_frost'
        template.class_name = 'deathknight'
        template.save(update_fields=['spec', 'class_name'])

        response = self.client.post('/api/simc-task/', data=json.dumps(
            self.task_payload(spec='frost_dk')
        ), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        frozen = json.loads(SimcTask.objects.get().ext)['player_equipment']
        self.assertIn('deathknight="Upstream Fury"', frozen)
        self.assertIn('spec=frost', frozen)
