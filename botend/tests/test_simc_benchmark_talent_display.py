"""Benchmark-only title projection must not change executable/frozen evidence."""
from copy import deepcopy
import json
from pathlib import Path

from django.test import TestCase

from botend.constants.hero_talents import normalize_hero_subtree_title_zh
from botend.guide_models import ClassGuide
from botend.models import SimcTalentString
from botend.services.simc_benchmark_config import build_execution_plan, serialize_panel_config
from botend.services.simc_benchmark_execution import _profile_detail_from_payload
from botend.services.simc_benchmark_targeting import plan_hash
from botend.services.simc_task_service import _build_talent_payload, _compute_content_hash
from botend.tests.simc_benchmark_test_fixtures import BenchmarkExecutionFixtureMixin


class BenchmarkTalentDisplayTests(BenchmarkExecutionFixtureMixin, TestCase):
    def test_config_display_name_is_separate_from_name_and_frozen_plan(self):
        self.talent.name = '[Icy Veins] ![icon](https://example.test/icon.webp) **Raid build**'
        self.talent.save(update_fields=['name'])
        original_name = self.talent.name
        frozen = _build_talent_payload(self.talent)
        original_hash = _compute_content_hash(frozen)
        plan = build_execution_plan(self.panel)
        original_plan_hash = plan_hash(plan)

        row = serialize_panel_config(self.panel)['specs'][0]['profiles'][0]

        self.assertEqual(row['talent_string_name'], original_name)
        self.assertEqual(row['talent_string_display_name'], '[Icy Veins] Raid build')
        self.assertEqual(row['label'], self.benchmark_profile.label)
        self.talent.refresh_from_db()
        self.assertEqual(self.talent.name, original_name)
        self.assertEqual(_build_talent_payload(self.talent), frozen)
        self.assertEqual(_compute_content_hash(_build_talent_payload(self.talent)), original_hash)
        after_plan = build_execution_plan(self.panel)
        self.assertEqual(after_plan, plan)
        self.assertEqual(plan_hash(after_plan), original_plan_hash)
        self.assertNotIn('display_name', after_plan['cases'][0]['resources']['talent_string'])
        self.assertNotIn('display_name', frozen)

    def test_frozen_profile_detail_gets_source_title_without_rewriting_payload(self):
        evidence = json.loads((Path(__file__).parent / 'fixtures' / 'simc_talent_titles_icy.json').read_text())
        resource = evidence['resources'][4]
        occurrences = [row for row in evidence['occurrences'] if row['guide_id'] == 105]
        first = occurrences[0]
        ClassGuide.objects.create(
            pk=105, title='Evidence', slug='benchmark-title-evidence',
            spec_id=first['spec_id'], class_name=first['class_name'], spec_name=first['spec_name'],
            game_version='12.1.5', source_url='https://www.icy-veins.com/wow/test',
            source_markdown='\n\n'.join(
                '\n\n'.join('#' * (index + 2) + ' ' + title
                            for index, title in enumerate(row['heading_path']))
                + '\n\n' + row['raw_block'] for row in occurrences
            ),
        )
        talent = SimcTalentString.objects.create(**{
            key: resource[key] for key in ('name', 'spec', 'talent', 'hero_talent_names', 'system_key')
        })
        payload = _build_talent_payload(talent)
        original = deepcopy(payload)
        frozen_hash = _compute_content_hash(payload)
        profile = {'spec': resource['spec'], 'talent': 'different-profile-default'}
        original_profile = deepcopy(profile)
        talent.name = 'CURRENT NAME MUST NOT REPLACE FROZEN EVIDENCE'
        talent.save(update_fields=['name'])

        detail = _profile_detail_from_payload(profile, resource['spec'], payload)

        self.assertIn('Outlaw Single Target - Trickster', detail['talents']['display_name'])
        self.assertNotIn('CURRENT NAME', detail['talents']['display_name'])
        # Keep the pre-existing legacy hero-name localization contract.
        self.assertEqual(detail['talents']['name'], normalize_hero_subtree_title_zh(original['name']))
        self.assertEqual(detail['talents']['build_code'], original['talent'])
        self.assertEqual(payload, original)
        self.assertEqual(profile, original_profile)
        self.assertEqual(_compute_content_hash(payload), frozen_hash)

        from botend.services.simc_benchmark_execution import (
            _coordinate_option, _project_profile_labels,
            serialize_incremental_panel_results, summarize_execution,
        )
        label = 'Profile · ' + original['name']
        coordinate = {'spec_key': resource['spec'], 'spec_label': 'Outlaw',
                      'scenario_key': 'raid', 'scenario_label': 'Raid',
                      'profile_key': 'frozen', 'profile_label': label,
                      'resources': {'talent_string': original}}
        before = deepcopy(coordinate)
        option = _coordinate_option(coordinate)
        self.assertIn('Profile · ', option['labels']['profile'])
        self.assertIn('Outlaw Single Target - Trickster', option['labels']['profile'])
        self.assertNotIn('Fatebound', option['labels']['profile'])
        self.assertNotIn('命缚者', option['labels']['profile'])
        self.assertEqual(option['labels']['talent'], option['labels']['profile'])
        self.assertEqual(coordinate, before)
        self.assertEqual(_project_profile_labels(['Custom raid'], [original])[0],
                         'Custom raid · ' + detail['talents']['display_name'])
        no_code = {**original, 'talent': ''}
        with self.assertNumQueries(0):
            self.assertNotIn('Outlaw Single Target', _project_profile_labels([label], [no_code])[0])
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            batch = _project_profile_labels([label] * 20, [original] * 20)
        self.assertLessEqual(len(queries), 2)
        self.assertTrue(all('Outlaw Single Target - Trickster' in title for title in batch))

        for obj in (self.profile, self.apl, self.template, self.talent):
            obj.spec = resource['spec']
            obj.save(update_fields=['spec'])
        self.profile.class_name = 'rogue'
        self.profile.save(update_fields=['class_name'])
        spec = self.benchmark_profile.panel_spec
        spec.spec_key, spec.class_name = resource['spec'], 'rogue'
        spec.save(update_fields=['spec_key', 'class_name'])
        self.talent.name, self.talent.talent = original['name'], original['talent']
        self.talent.save(update_fields=['name', 'talent'])
        self.benchmark_profile.label = '**Frozen custom**'
        self.benchmark_profile.save(update_fields=['label'])
        self.profile.name = '**Frozen profile**'
        self.profile.save(update_fields=['name'])
        execution = self._published_success()
        execution.refresh_from_db()
        snapshot, config_hash, result_hash = (deepcopy(execution.config_snapshot),
                                             execution.config_hash, execution.result_hash)
        summary = summarize_execution(execution)
        self.assertEqual(summary['status'], 'success')
        self.assertNotIn('**', summary['cases'][0]['labels']['profile'])
        self.assertIn('Outlaw Single Target - Trickster', summary['cases'][0]['labels']['profile'])
        before_plan = build_execution_plan(self.panel)
        results = serialize_incremental_panel_results(self.panel, include_coordinate_options=True)
        for row in results['coordinates'] + results['coordinate_options']:
            self.assertIn('Outlaw Single Target - Trickster', row['labels']['profile'])
            self.assertNotIn('**', row['labels']['profile'])
            self.assertEqual(row['labels']['profile'], row['labels']['talent'])
        execution.refresh_from_db()
        self.assertEqual(execution.config_snapshot, snapshot)
        self.assertEqual((execution.config_hash, execution.result_hash), (config_hash, result_hash))
        from botend.services.simc_benchmark_execution import serialize_public_execution
        public = serialize_public_execution(execution)
        self.assertEqual(public['status'], 'ready')
        self.assertNotIn('**', public['execution']['cases'][0]['labels']['profile'])
        self.assertEqual(build_execution_plan(self.panel), before_plan)
        self.assertEqual(plan_hash(build_execution_plan(self.panel)), plan_hash(before_plan))
        self.talent.name = 'CURRENT RESOURCE NAME'
        self.talent.talent = 'changed-current-code'
        self.talent.save(update_fields=['name', 'talent'])
        history = summarize_execution(execution)
        self.assertEqual(history, summary)
        execution.cases.update(status='pending')
        execution.status = 'pending'
        execution.save(update_fields=['status'])
        active = summarize_execution(execution)
        self.assertIn('Outlaw Single Target - Trickster', active['cases'][0]['labels']['profile'])

        legacy = _profile_detail_from_payload({}, 'warrior_fury', fallback_talent_name='Legacy build')
        self.assertEqual(legacy['talents']['name'], 'Legacy build')
        self.assertEqual(legacy['talents']['display_name'], 'Legacy build')
        self.assertEqual(legacy['talents']['build_code'], '')
