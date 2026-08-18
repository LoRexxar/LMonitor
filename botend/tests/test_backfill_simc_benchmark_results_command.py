from io import StringIO
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError

from botend.models import (
    SimcBenchmarkExecution, SimcBenchmarkResult, SimcBenchmarkCandidate, SimulationRun,
    WowItemSnapshot,
)
from botend.tests.test_simc_benchmark_execution import SimcBenchmarkExecutionTests


class BackfillSimcBenchmarkResultsCommandTests(SimcBenchmarkExecutionTests):
    def test_requires_existing_execution(self):
        with self.assertRaises(CommandError):
            call_command('backfill_simc_benchmark_results', execution_id=99999)

    def test_backfills_successful_case_rows(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline', dps=1234)
        self._run(task, 2, 'completed', 'trinket', dps=1256)
        from botend.services.simc_benchmark_execution import reconcile_execution
        reconcile_execution(execution)
        SimcBenchmarkResult.objects.filter(case__execution=execution).delete()

        output = StringIO()
        call_command('backfill_simc_benchmark_results', execution_id=execution.pk, stdout=output)

        self.assertIn('backfilled 2 result rows', output.getvalue())
        self.assertEqual(execution.cases.get().results.count(), 2)

    def test_backfills_existing_candidate_and_run_display_metadata(self):
        execution = self._create()
        task = execution.cases.get().task
        candidate = SimcBenchmarkCandidate.objects.get(panel=execution.panel, key='trinket')
        gear_swap = {
            'item_id': 248583,
            'slot': 'trinket1',
            'source': 'manual',
            'raw_value': ',id=248583,ilevel=285,bonus_id=13183',
        }
        candidate.params['gear_swap'].update(gear_swap)
        candidate.save(update_fields=['params'])
        execution.config_snapshot['candidates'][1]['params']['gear_swap'].update(gear_swap)
        execution.save(update_fields=['config_snapshot'])
        run = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='trinket', candidate_label='Trinket',
            candidate_params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': gear_swap,
            },
        )
        # Stale recovery/retry atomically rebinds the Case to a replacement Task;
        # the original Task and its Runs remain valid Benchmark history.
        benchmark_case = execution.cases.get()
        benchmark_case.task = None
        benchmark_case.save(update_fields=['task'])
        self.assertEqual(candidate.label, 'Trinket')
        self.assertEqual(run.candidate_label, 'Trinket')
        self.assertFalse(run.candidate_params.get('icon_url'))
        self.assertFalse(run.display_metadata.get('icon_url'))
        WowItemSnapshot.objects.create(
            item_id=248583, name='Drum of Renewed Bonds', name_zh='焕新羁绊之鼓', icon='inv_trinket_raid_01',
        )

        snapshot_before = deepcopy(execution.config_snapshot)
        hash_before = execution.config_hash
        output = StringIO()
        call_command('backfill_simc_benchmark_display_metadata', stdout=output)

        candidate.refresh_from_db()
        run.refresh_from_db()
        execution.refresh_from_db()
        self.assertEqual(candidate.label, '焕新羁绊之鼓 · 暴击')
        self.assertEqual(candidate.icon_url, '/static/wow_icons/small/inv_trinket_raid_01.jpg')
        self.assertEqual(run.candidate_label, '焕新羁绊之鼓 · 暴击')
        self.assertEqual(
            run.display_metadata['icon_url'],
            '/static/wow_icons/small/inv_trinket_raid_01.jpg',
        )
        self.assertEqual(execution.config_snapshot, snapshot_before)
        self.assertEqual(execution.config_hash, hash_before)
        self.assertEqual(execution.display_metadata['trinket'], {
            'label': '焕新羁绊之鼓 · 暴击',
            'icon_url': '/static/wow_icons/small/inv_trinket_raid_01.jpg',
        })
        self.assertIn('updated 1 candidates, 1 runs, and 1 executions', output.getvalue())

        repeat = StringIO()
        call_command('backfill_simc_benchmark_display_metadata', stdout=repeat)
        self.assertIn('updated 0 candidates, 0 runs, and 0 executions', repeat.getvalue())

    def test_backfill_prefers_chinese_tooltip_over_more_verbose_english_snapshot(self):
        self.benchmark_profile.talent_string = self.talent
        self.benchmark_profile.save(update_fields=['talent_string'])
        execution = self._create()
        case = execution.cases.get()
        candidate = SimcBenchmarkCandidate.objects.get(panel=execution.panel, key='trinket')
        candidate.effect = 'Equip: stale English effect with more verbose frozen content.'
        candidate.save(update_fields=['effect'])
        execution.display_metadata = {
            'trinket': {'effect': 'Equip: stale execution English effect with more verbose frozen content.'},
        }
        execution.save(update_fields=['display_metadata'])
        run = SimulationRun.objects.create(
            task=case.task, sequence=1, candidate_key='trinket', candidate_label='Trinket',
            candidate_params=candidate.params,
            display_metadata={'effect': 'Equip: stale run English effect with more verbose frozen content.'},
        )
        WowItemSnapshot.objects.create(
            item_id=123, name='Test Trinket', name_zh='测试饰品',
            description='Equip: A much longer English static effect description with extra details.',
            description_zh='装备：中文特效。',
        )
        snapshot_before = deepcopy(execution.config_snapshot)
        hash_before = execution.config_hash

        call_command(
            'backfill_simc_benchmark_display_metadata', panel_slug=execution.panel.slug, batch_size=1,
        )

        candidate.refresh_from_db()
        run.refresh_from_db()
        execution.refresh_from_db()
        self.assertEqual(candidate.effect, '装备：中文特效。')
        self.assertEqual(run.display_metadata['effect'], '装备：中文特效。')
        self.assertEqual(execution.display_metadata['trinket']['effect'], '装备：中文特效。')
        self.assertEqual(execution.config_snapshot, snapshot_before)
        self.assertEqual(execution.config_hash, hash_before)

    def test_candidate_level_tooltips_are_exact_and_preserve_results(self):
        execution = self._create()
        case = execution.cases.get()
        candidate = SimcBenchmarkCandidate.objects.get(panel=execution.panel, key='trinket')
        gear_swap = {
            'item_id': 270160,
            'slot': 'trinket1',
            'source': 'ptr_db2',
            'raw_value': ',id=270160,ilevel=285',
        }
        candidate.params['gear_swap'].update(gear_swap)
        candidate.effect = '装备：旧快照在 219 装等造成 100 点伤害。'
        candidate.save(update_fields=['params', 'effect'])
        execution.config_snapshot['candidates'][1]['params']['gear_swap'].update(gear_swap)
        execution.display_metadata = {
            'trinket': {'effect': '装备：旧执行快照在 219 装等造成 100 点伤害。'},
        }
        execution.save(update_fields=['config_snapshot', 'display_metadata'])
        run = SimulationRun.objects.create(
            task=case.task, sequence=1, candidate_key='trinket', candidate_label='Trinket',
            candidate_params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': gear_swap,
            },
            display_metadata={'effect': '装备：旧 Run 快照在 219 装等造成 100 点伤害。'},
        )
        result = SimcBenchmarkResult.objects.create(
            case=case, candidate_key='trinket', dps=4321.5,
        )
        WowItemSnapshot.objects.create(
            item_id=270160, name='Test Trinket', name_zh='测试饰品',
            description_zh='装备：静态物品快照在 219 装等造成 100 点伤害。',
        )
        tooltip_payload = {
            'schema_version': 1,
            'source': {
                'wago_build': '12.1.0.69189',
                'wago_locale': 'zhCN',
                'simc_build': '12.1.0.69189',
                'simc_revision': 'fd9816d69067',
            },
            'tooltips': [{
                'item_id': 270160,
                'item_level': 285,
                'description_zh': '装备：造成 9876 点火焰伤害。',
                'spell_ids': [123456],
            }],
        }
        snapshot_before = deepcopy(execution.config_snapshot)
        hash_before = execution.config_hash
        results_before = list(
            SimcBenchmarkResult.objects.filter(case__execution=execution)
            .values_list('id', 'candidate_key', 'dps')
        )

        with TemporaryDirectory() as temp_dir:
            tooltip_path = Path(temp_dir) / 'tooltips.json'
            tooltip_path.write_text(json.dumps(tooltip_payload), encoding='utf-8')
            call_command(
                'backfill_simc_benchmark_display_metadata',
                panel_slug=execution.panel.slug,
                tooltip_data=str(tooltip_path),
            )

        candidate.refresh_from_db()
        run.refresh_from_db()
        execution.refresh_from_db()
        result.refresh_from_db()
        expected = '装备：造成 9876 点火焰伤害。'
        self.assertEqual(candidate.effect, expected)
        self.assertEqual(run.display_metadata['effect'], expected)
        self.assertEqual(execution.display_metadata['trinket']['effect'], expected)
        self.assertNotIn('219', candidate.effect)
        self.assertEqual(execution.config_snapshot, snapshot_before)
        self.assertEqual(execution.config_hash, hash_before)
        self.assertEqual(
            list(
                SimcBenchmarkResult.objects.filter(case__execution=execution)
                .values_list('id', 'candidate_key', 'dps')
            ),
            results_before,
        )
        self.assertEqual(result.dps, 4321.5)

    def test_candidate_level_tooltip_does_not_require_item_snapshot(self):
        execution = self._create()
        candidate = SimcBenchmarkCandidate.objects.get(panel=execution.panel, key='trinket')
        candidate.params['gear_swap'].update({
            'item_id': 270160,
            'raw_value': ',id=270160,ilevel=285',
        })
        candidate.save(update_fields=['params'])
        tooltip_payload = {
            'schema_version': 1,
            'source': {
                'wago_build': '12.1.0.69189',
                'wago_locale': 'zhCN',
                'simc_build': '12.1.0.69189',
                'simc_revision': 'fd9816d69067',
            },
            'tooltips': [{
                'item_id': 270160,
                'item_level': 285,
                'description_zh': '装备：造成 9876 点火焰伤害。',
                'spell_ids': [123456],
            }],
        }

        with TemporaryDirectory() as temp_dir:
            tooltip_path = Path(temp_dir) / 'tooltips.json'
            tooltip_path.write_text(json.dumps(tooltip_payload), encoding='utf-8')
            call_command(
                'backfill_simc_benchmark_display_metadata',
                panel_slug=execution.panel.slug,
                tooltip_data=str(tooltip_path),
            )

        candidate.refresh_from_db()
        self.assertEqual(candidate.effect, '装备：造成 9876 点火焰伤害。')

    def test_schema_v2_backfills_exact_metadata_without_item_snapshot(self):
        execution = self._create()
        case = execution.cases.get()
        candidate = SimcBenchmarkCandidate.objects.get(panel=execution.panel, key='trinket')
        gear_swap = {
            'item_id': 268292, 'slot': 'trinket1', 'source': 'ptr_db2',
            'raw_value': ',id=268292,ilevel=285',
        }
        candidate.params['gear_swap'].update(gear_swap)
        candidate.save(update_fields=['params'])
        execution.config_snapshot['candidates'][1]['params']['gear_swap'].update(gear_swap)
        execution.save(update_fields=['config_snapshot'])
        run = SimulationRun.objects.create(
            task=case.task, sequence=2, candidate_key='trinket', candidate_label='old',
            candidate_params={'candidate_type': 'gear_swap', 'gear_swap': gear_swap},
        )
        payload = {
            'schema_version': 2,
            'source': {
                'wago_build': '12.1.0.69189', 'wago_locale': 'zhCN',
                'simc_build': '12.1.0.69189', 'simc_revision': 'fd9816d69067',
            },
            'tooltips': [{
                'item_id': 268292, 'item_level': 285,
                'name_zh': '菌丝聚合器', 'icon_file_data_id': 7702761,
                'icon_name': 'inv_1207_fungarianraid_trinket',
                'icon_url': '/static/wow_icons/small/inv_1207_fungarianraid_trinket.jpg',
                'stats': [{'key': 'stragiint', 'value': 128, 'text': '+128 力量/敏捷/智力'}],
                'effects': ['装备：造成 9,876 点自然伤害。'],
                'description_zh': '装备：造成 9,876 点自然伤害。',
                'spell_ids': [123456], 'unresolved_tokens': [],
            }],
        }
        snapshot_before = deepcopy(execution.config_snapshot)
        hash_before = execution.config_hash

        with TemporaryDirectory() as temp_dir:
            tooltip_path = Path(temp_dir) / 'tooltips.json'
            tooltip_path.write_text(json.dumps(payload), encoding='utf-8')
            call_command(
                'backfill_simc_benchmark_display_metadata',
                panel_slug=execution.panel.slug, tooltip_data=str(tooltip_path),
            )

        candidate.refresh_from_db()
        run.refresh_from_db()
        execution.refresh_from_db()
        effect = '+128 力量/敏捷/智力\n装备：造成 9,876 点自然伤害。'
        icon_url = '/static/wow_icons/small/inv_1207_fungarianraid_trinket.jpg'
        self.assertEqual(
            (candidate.label, candidate.icon_url, candidate.effect),
            ('菌丝聚合器', icon_url, effect),
        )
        self.assertEqual(run.candidate_label, '菌丝聚合器')
        self.assertEqual(run.display_metadata['icon_url'], icon_url)
        self.assertEqual(run.display_metadata['effect'], effect)
        self.assertEqual(execution.display_metadata['trinket'], {
            'label': '菌丝聚合器', 'icon_url': icon_url, 'effect': effect,
        })
        self.assertEqual(execution.config_snapshot, snapshot_before)
        self.assertEqual(execution.config_hash, hash_before)

    def test_unresolved_tooltips_require_explicit_opt_in(self):
        execution = self._create()
        payload = {
            'schema_version': 1,
            'source': {
                'wago_build': '12.1.0.69189',
                'wago_locale': 'zhCN',
                'simc_build': '12.1.0.69189',
                'simc_revision': 'fd9816d69067',
            },
            'tooltips': [{
                'item_id': 270160,
                'item_level': 285,
                'description_zh': '装备：造成${100*$<rolemult>}点伤害。',
                'spell_ids': [123456],
                'unresolved_tokens': ['$<rolemult>'],
            }],
        }
        with TemporaryDirectory() as temp_dir:
            tooltip_path = Path(temp_dir) / 'tooltips.json'
            tooltip_path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaisesMessage(CommandError, 'unresolved token'):
                call_command(
                    'backfill_simc_benchmark_display_metadata',
                    panel_slug=execution.panel.slug,
                    tooltip_data=str(tooltip_path),
                )
            call_command(
                'backfill_simc_benchmark_display_metadata',
                panel_slug=execution.panel.slug,
                tooltip_data=str(tooltip_path),
                allow_unresolved_tooltips=True,
            )
