import importlib

from django.apps import apps as global_apps
from django.test import TestCase

from botend.models import (
    SimcApl,
    SimcBackendBinary,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkSpec,
    SimcContentTemplate,
    SimcProfile,
    SimcTalentString,
)


class BenchmarkTalentStringBackfillTests(TestCase):
    def test_only_unique_exact_profile_talent_match_is_backfilled(self):
        backend = SimcBackendBinary.objects.create(
            identifier='backfill-test', name='Backfill Test', current_version='simc-test',
        )
        apl = SimcApl.objects.create(
            name='Backfill APL', spec='warrior_fury', class_name='warrior',
            content='actions=auto_attack', owner_user_id=1,
        )
        template = SimcContentTemplate.objects.create(
            name='Backfill Template', spec='default', content='iterations=1', owner_user_id=1,
        )
        panel = SimcBenchmarkPanel.objects.create(
            name='Backfill Panel', slug='backfill-panel', created_by_id=1,
        )
        panel_spec = SimcBenchmarkSpec.objects.create(
            panel=panel, class_name='warrior', spec_key='warrior_fury', label='狂怒',
            apl=apl, template=template, backend=backend,
        )

        exact_profile = SimcProfile.objects.create(
            user_id=1, name='Exact', class_name='warrior', spec='fury', talent='EXACT',
        )
        missing_profile = SimcProfile.objects.create(
            user_id=1, name='Missing', class_name='warrior', spec='fury', talent='MISSING',
        )
        duplicate_profile = SimcProfile.objects.create(
            user_id=1, name='Duplicate', class_name='warrior', spec='fury', talent='DUPLICATE',
        )
        exact_talent = SimcTalentString.objects.create(
            name='Exact Talent', spec='warrior_fury', talent='EXACT',
        )
        SimcTalentString.objects.create(
            name='Duplicate Talent A', spec='warrior_fury', talent='DUPLICATE',
        )
        SimcTalentString.objects.create(
            name='Duplicate Talent B', spec='warrior_fury', talent='DUPLICATE',
        )
        rows = [
            SimcBenchmarkProfile.objects.create(
                panel_spec=panel_spec, profile=profile, label=profile.name, display_order=order,
            )
            for order, profile in enumerate((exact_profile, missing_profile, duplicate_profile))
        ]

        migration = importlib.import_module(
            'botend.migrations.0171_backfill_simc_benchmark_profile_talent_strings'
        )
        migration.backfill_benchmark_profile_talent_strings(global_apps, None)

        for row in rows:
            row.refresh_from_db()
        self.assertEqual(rows[0].talent_string_id, exact_talent.id)
        self.assertIsNone(rows[1].talent_string_id)
        self.assertIsNone(rows[2].talent_string_id)
