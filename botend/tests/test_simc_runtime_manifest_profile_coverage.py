"""Updater argv/cleanup contracts; only the external SimC process is doubled."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import TestCase

from botend.management.commands.update_simc_binary import Command
from botend.models import SimcApl, SimcBackendBinary, SimcProfile
from botend.services.simc_apl.symbol_sync import load_runtime_manifest


REVISION = 'a' * 40
BUILD = '12.1.5.70575'


class RuntimeManifestProfileCoverageTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.command = Command()
        self.command.simc_source_dir = str(self.source)
        # Deliberately nonexistent: these tests must never start a native process.
        self.command.simc_binary_path = str(self.source / 'not-a-real-simc')
        self.command.row, _ = SimcBackendBinary.objects.update_or_create(
            identifier='runtime-manifest-test',
            defaults={'name': 'Runtime manifest test', 'platform': 'linux64'},
        )
        self.captured = {}

    def profile(self, profile_set, spec, suffix=''):
        path = self.source / 'profiles' / profile_set / f'{profile_set}_Druid_{spec}{suffix}.simc'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.baseline_content(spec), encoding='utf-8')
        return path

    @staticmethod
    def baseline_content(spec):
        return f'druid="{spec}"\nlevel=90\nspec={spec.lower()}\nhead=,id=1\nmain_hand=,id=2\n'

    def database_baseline(self, spec):
        return SimcProfile.objects.create(
            name=spec, class_name='druid', spec=f'druid_{spec.lower()}',
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            sync_version=REVISION, is_active=True,
            player_config_mode='manual_equipment',
            player_equipment=self.baseline_content(spec),
        )

    def required_apl(self, spec):
        return SimcApl.objects.create(
            name=spec, class_name='druid', spec=f'druid_{spec.lower()}',
            content='actions=/wrath', source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True, is_active=True, sync_version=REVISION,
        )

    def export_process_double(self, argv, **kwargs):
        self.captured['argv'] = argv
        self.captured['kwargs'] = kwargs
        inputs = [Path(value) for value in argv[1:] if '=' not in value]
        self.captured['inputs'] = inputs
        self.captured['contents'] = {path: path.read_text(encoding='utf-8') for path in inputs}
        self.captured['generated'] = [path for path in inputs if path.parent != self.source / 'profiles' / 'MID1'
                                      and path.parent != self.source / 'profiles' / 'MID2']
        target = Path(next(value.split('=', 1)[1] for value in argv
                           if value.startswith('apl_metadata_export=')))
        self.captured['manifest'] = target
        target.write_text(json.dumps({
            'schema_version': 1, 'simc_revision': REVISION, 'game_build': BUILD,
            'generated_at': '2026-11-12T00:00:00Z',
            'completeness': {'status': 'partial', 'modules': {
                'global_options': 'runtime_initialized', 'actions': 'runtime_observed',
                'action_options': 'runtime_observed', 'expressions': 'runtime_observed',
                'class_specs': 'runtime_initialized',
            }, 'limitations': ['Protocol fixture, not native execution.']},
            'symbols': [{'class': 'druid', 'spec': 'balance', 'scope': 'spec',
                         'token': 'wrath', 'kind': 'action', 'spell_id': 190984,
                         'source': 'runtime_action', 'options': [], 'aliases': []}],
        }), encoding='utf-8')

    def export(self):
        with patch.object(self.command, '_run', side_effect=self.export_process_double) as run:
            result = self.command._export_runtime_manifest(REVISION, BUILD)
        self.addCleanup(lambda: Path(result).unlink(missing_ok=True))
        run.assert_called_once()
        self.assertEqual(self.captured['argv'][0], self.command.simc_binary_path)
        self.assertEqual(self.captured['kwargs'], {
            'cwd': str(self.source), 'timeout': 120,
            'status': '导出 SimC runtime APL manifest', 'progress': 92,
        })
        self.assertEqual(self.captured['argv'][-3:], [
            f'apl_metadata_export={result}', f'apl_metadata_revision={REVISION}',
            f'apl_metadata_game_build={BUILD}',
        ])
        load_runtime_manifest(result, REVISION, BUILD)
        self.assertTrue(all(not path.exists() for path in self.captured['generated']))
        return self.captured['inputs']

    def test_mid2_feral_keeps_hero_variants_and_adds_mid1_balance_guardian(self):
        feral = self.profile('MID2', 'Feral')
        hero = self.profile('MID2', 'Feral', '_DruidOfTheClaw')
        balance = self.profile('MID1', 'Balance')
        guardian = self.profile('MID1', 'Guardian')
        duplicate = self.profile('MID1', 'Feral')
        for spec in ('Feral', 'Balance', 'Guardian'):
            self.database_baseline(spec)
            self.required_apl(spec)

        inputs = self.export()

        self.assertEqual(inputs, [feral, hero, balance, guardian])
        self.assertNotIn(duplicate, inputs)
        self.assertEqual(self.captured['generated'], [])

    def test_mid2_hero_filename_does_not_alias_base_spec_coverage(self):
        hero = self.profile('MID2', 'Balance', '_KeeperOfTheGrove')
        balance = self.profile('MID1', 'Balance')
        self.database_baseline('Balance')
        self.required_apl('Balance')

        self.assertEqual(self.export(), [hero, balance])

    def test_database_baseline_without_source_file_still_generates_actor(self):
        feral = self.profile('MID2', 'Feral')
        baseline = self.database_baseline('Balance')
        self.required_apl('Balance')
        # Do not treat another revision or inactive APL as a required actor.
        ignored = self.required_apl('Guardian')
        ignored.sync_version = 'b' * 40
        ignored.save(update_fields=['sync_version'])

        inputs = self.export()

        self.assertEqual(inputs[0], feral)
        self.assertEqual(len(self.captured['generated']), 1)
        generated = self.captured['generated'][0]
        self.assertEqual(self.captured['contents'][generated], baseline.player_equipment)

    def test_missing_mid2_directory_retains_mid1_inputs(self):
        balance = self.profile('MID1', 'Balance')
        hero = self.profile('MID1', 'Balance', '_ElunesChosen')

        self.assertEqual(self.export(), [balance, hero])

    def test_export_failure_and_wrong_identity_remove_manifest_and_generated_inputs(self):
        self.profile('MID2', 'Feral')
        self.database_baseline('Balance')
        self.required_apl('Balance')
        for failure in ('process', 'revision', 'build'):
            with self.subTest(failure=failure):
                def failed_export(argv, **kwargs):
                    self.export_process_double(argv, **kwargs)
                    if failure == 'process':
                        raise CommandError('external export failed')
                    target = self.captured['manifest']
                    payload = json.loads(target.read_text(encoding='utf-8'))
                    payload['simc_revision' if failure == 'revision' else 'game_build'] = 'wrong'
                    target.write_text(json.dumps(payload), encoding='utf-8')

                with patch.object(self.command, '_run', side_effect=failed_export):
                    with self.assertRaises(CommandError):
                        self.command._export_runtime_manifest(REVISION, BUILD)
                self.assertFalse(self.captured['manifest'].exists())
                self.assertEqual(len(self.captured['generated']), 1)
                self.assertTrue(all(not path.exists() for path in self.captured['generated']))
