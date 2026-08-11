from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from botend.models import (
    SimcApl,
    SimcBackendBinary,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkSpec,
    SimcContentTemplate,
    SimcProfile,
)


OLD_RING = (
    'finger1=,id=268290,ilevel=334,gem_id=240890,enchant_id=7967,'
    'bonus_id=41/13668/13335/13786'
)
NEW_RING = (
    'finger1=,id=268249,ilevel=334,gem_id=240890,enchant_id=7967,'
    'bonus_id=40/13668/13335/12854'
)
TARGET_PANELS = (
    'ptr-12-1-default-scenarios',
    '121-ptr-0540ce8c2bbb435a832160525c4cb668',
)
TARGET_SPECS = (
    ('hunter_marksmanship', 'hunter'),
    ('hunter_survival', 'hunter'),
    ('warlock_affliction', 'warlock'),
)


class RepairPtrBenchmarkProfilesTests(TestCase):
    def test_switches_six_profiles_to_builtin_apls_and_repairs_affliction_ring_idempotently(self):
        owner = User.objects.create_user(username='benchmark-owner')
        backend = SimcBackendBinary.objects.create(
            identifier='ptr-test', name='PTR test', current_version='revision',
        )
        template = SimcContentTemplate.objects.create(
            name='PTR template', spec='default', content='{player_identity}\n{action_list}',
        )
        old_apls = {}
        for spec_key, class_name in TARGET_SPECS:
            old_apls[spec_key] = SimcApl.objects.create(
                name=f'Live APL {spec_key}', spec=spec_key, class_name=class_name,
                content='actions=/auto_attack', is_system=False,
                owner_user_id=owner.id,
            )

        original_hunter_equipment = 'head=,id=1\nfinger1=,id=268249,ilevel=334'
        stored_classes = {
            ('ptr-12-1-default-scenarios', 'hunter_marksmanship'): 'hunter',
            ('ptr-12-1-default-scenarios', 'hunter_survival'): 'hunter_survival',
            ('ptr-12-1-default-scenarios', 'warlock_affliction'): 'warlock_affliction',
            ('121-ptr-0540ce8c2bbb435a832160525c4cb668', 'hunter_marksmanship'): 'hunter_marksmanship',
            ('121-ptr-0540ce8c2bbb435a832160525c4cb668', 'hunter_survival'): 'hunter_survival',
            ('121-ptr-0540ce8c2bbb435a832160525c4cb668', 'warlock_affliction'): 'warlock',
        }
        profile_ids = []
        for panel_slug in TARGET_PANELS:
            panel = SimcBenchmarkPanel.objects.create(
                name=panel_slug, slug=panel_slug, created_by_id=owner.id,
            )
            for order, (spec_key, class_name) in enumerate(TARGET_SPECS):
                panel_spec = SimcBenchmarkSpec.objects.create(
                    panel=panel, class_name=class_name, spec_key=spec_key,
                    label=spec_key, apl=old_apls[spec_key], template=template,
                    backend=backend, display_order=order,
                )
                equipment = (
                    f'head=,id=1\n{OLD_RING}\nfinger2=,id=268252,ilevel=334'
                    if spec_key == 'warlock_affliction'
                    else original_hunter_equipment
                )
                profile = SimcProfile.objects.create(
                    user_id=owner.id,
                    name=f'12.1 PTR {panel_slug} {spec_key}',
                    class_name=stored_classes[(panel_slug, spec_key)],
                    version='12.1',
                    use_ptr=True,
                    spec=spec_key,
                    player_config_mode='manual_equipment',
                    player_equipment=equipment,
                    talent='test-talent',
                )
                profile_ids.append(profile.id)
                SimcBenchmarkProfile.objects.create(
                    panel_spec=panel_spec, profile=profile, label=profile.name,
                )

        output = StringIO()
        call_command('repair_ptr_benchmark_profiles', apply=True, stdout=output)
        call_command('repair_ptr_benchmark_profiles', apply=True, stdout=output)

        self.assertEqual(
            SimcApl.objects.filter(source='simc_builtin', content='').count(), 3,
        )
        specs = SimcBenchmarkSpec.objects.filter(
            panel__slug__in=TARGET_PANELS,
            spec_key__in=[item[0] for item in TARGET_SPECS],
        ).select_related('apl')
        self.assertEqual(specs.count(), 6)
        for panel_spec in specs:
            self.assertEqual(panel_spec.apl.source, 'simc_builtin')
            self.assertEqual(panel_spec.apl.spec, panel_spec.spec_key)
            self.assertEqual(panel_spec.apl.content, '')
            self.assertTrue(panel_spec.apl.is_system)
            self.assertTrue(panel_spec.apl.is_selectable)

        profiles = SimcProfile.objects.filter(id__in=profile_ids)
        self.assertEqual(profiles.count(), 6)
        for profile in profiles:
            if profile.spec == 'warlock_affliction':
                self.assertNotIn(OLD_RING, profile.player_equipment)
                self.assertIn(NEW_RING, profile.player_equipment)
            else:
                self.assertEqual(profile.player_equipment, original_hunter_equipment)
        self.assertIn('changed_profiles=2', output.getvalue())
        self.assertIn('changed_specs=6', output.getvalue())
