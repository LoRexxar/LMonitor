from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from botend.models import SimcProfile
from botend.services.simc_composer import SimcComposer


FURY_CONSUMABLES = (
    'potion=lights_potential_2',
    'flask=flask_of_the_magisters_2',
    'food=harandar_celebration',
    'augmentation=void_touched',
    'temporary_enchant=main_hand:thalassian_phoenix_oil_2/'
    'off_hand:thalassian_phoenix_oil_2',
)


class SyncPtrProfileConsumablesTests(TestCase):
    def test_copies_matching_mid1_consumables_to_both_fury_ptr_profiles_idempotently(self):
        SimcProfile.objects.create(
            user_id=None,
            name='MID1 默认玩家 warrior_fury',
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            class_name='warrior',
            version='12.0',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment='\n'.join((
                'warrior="MID1_Warrior_Fury"',
                'spec=fury',
                'level=90',
                *FURY_CONSUMABLES,
                'head=,id=1',
                'main_hand=,id=2',
            )),
        )
        single_target = SimcProfile.objects.create(
            user_id=None,
            name='12.1 PTR纯单体天赋 Warrior Fury',
            source=SimcProfile.SOURCE_WCL,
            class_name='warrior_fury',
            version='12.1',
            use_ptr=True,
            spec='warrior_fury',
            player_config_mode='wcl',
            player_equipment='\n'.join((
                '# WCL single target',
                'ptr=1',
                'warrior=MID2_Warrior_Fury',
                'spec=fury',
                '# Gear',
                'head=,id=10',
                'main_hand=,id=20',
            )),
        )
        dungeon_target = SimcProfile.objects.create(
            user_id=1,
            name='12.1 PTR大秘境天赋 Warrior Fury',
            source=SimcProfile.SOURCE_WCL,
            class_name='warrior',
            version='12.1',
            use_ptr=True,
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment='\n'.join((
                '# WCL dungeon',
                'ptr=1',
                'warrior=MID2_Warrior_Fury',
                'spec=fury',
                '# Gear',
                'head=,id=11',
                'main_hand=,id=21',
            )),
        )
        attribute_target = SimcProfile.objects.create(
            user_id=1,
            name='12.1 PTR大秘境天赋-属性强制版',
            source=SimcProfile.SOURCE_WCL,
            class_name='warrior',
            version='12.1',
            use_ptr=True,
            spec='warrior_fury',
            player_config_mode='attribute_only',
            player_equipment='\n'.join((
                '# Frozen attribute baseline',
                'ptr=1',
                'warrior=MID2_Warrior_Fury',
                'spec=fury',
                '# Gear',
                'head=,id=12',
                'main_hand=,id=22',
            )),
        )
        unmatched = SimcProfile.objects.create(
            user_id=None,
            name='12.1 PTR大秘境天赋 Priest Holy',
            source=SimcProfile.SOURCE_WCL,
            class_name='priest_holy',
            version='12.1',
            use_ptr=True,
            spec='priest_holy',
            player_config_mode='wcl',
            player_equipment='priest=PTR_Holy\nspec=holy\nmain_hand=,id=30',
        )

        dry_run = StringIO()
        call_command('sync_ptr_profile_consumables', stdout=dry_run)
        single_target.refresh_from_db()
        self.assertNotIn(FURY_CONSUMABLES[0], single_target.player_equipment)
        self.assertIn('DRY RUN: targets=4 matched=3 unmatched=1 changed_profiles=3', dry_run.getvalue())

        first_apply = StringIO()
        call_command('sync_ptr_profile_consumables', apply=True, stdout=first_apply)
        first_contents = {}
        for target in (single_target, dungeon_target, attribute_target):
            target.refresh_from_db()
            first_contents[target.pk] = target.player_equipment
            for line in FURY_CONSUMABLES:
                self.assertEqual(target.player_equipment.splitlines().count(line), 1)
            self.assertLess(
                target.player_equipment.splitlines().index(FURY_CONSUMABLES[0]),
                target.player_equipment.splitlines().index('# Gear'),
            )
        unmatched.refresh_from_db()
        self.assertEqual(
            unmatched.player_equipment,
            'priest=PTR_Holy\nspec=holy\nmain_hand=,id=30',
        )
        composed = SimcComposer(attribute_target.user_id).compose_validation_input(
            attribute_target, ''
        )
        for line in FURY_CONSUMABLES:
            self.assertEqual(composed.splitlines().count(line), 1)
        self.assertIn('changed_profiles=3', first_apply.getvalue())

        second_apply = StringIO()
        call_command('sync_ptr_profile_consumables', apply=True, stdout=second_apply)
        for target in (single_target, dungeon_target, attribute_target):
            target.refresh_from_db()
            self.assertEqual(target.player_equipment, first_contents[target.pk])
        self.assertIn('changed_profiles=0', second_apply.getvalue())
