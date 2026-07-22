import hashlib
import json
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from botend.management.commands.update_simc_binary import Command
from botend.models import SimcApl, SimcContentTemplate


@override_settings(SIMC_CONFIG={'wow_build': '12.0.1.70000'})
class SimcGeneratedInputSyncTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name)
        (self.source / 'ActionPriorityLists' / 'default').mkdir(parents=True)
        (self.source / 'profiles' / 'MID1').mkdir(parents=True)
        self.command = Command()
        self.command.simc_source_dir = str(self.source)
        self.command.simc_binary_path = '/tmp/new-simc'
        self.command.stdout = StringIO()

    def test_sync_inputs_only_promotes_matching_legacy_short_revision(self):
        revision = '62ababb127bef2a35f96357968d455dde7de7616'
        command = Command()
        command.platform = 'linux64'
        command.simc_source_dir = str(self.source)
        command.simc_build_dir = str(self.source / 'build-cli')
        command.simc_binary_path = '/tmp/new-simc'
        command.wow_build_override = ''
        command.row = SimpleNamespace(
            current_version='1205-01-62ababb', save=mock.Mock())
        command._set_status = mock.Mock()
        command._sync_generated_inputs = mock.Mock()

        with mock.patch.object(command, '_resolve_paths', return_value=(
                command.simc_source_dir, command.simc_build_dir, command.simc_binary_path)), \
                mock.patch.object(command, '_get_row', return_value=command.row), \
                mock.patch.object(command, '_get_git_hash', return_value=revision), \
                mock.patch('builtins.open', mock.mock_open()), \
                mock.patch('fcntl.flock'):
            command.handle(
                check=False, sync_inputs_only=True, apply_patches=False,
                no_pull=True, threads=1, wow_build='')

        self.assertEqual(command.row.current_version, revision)
        command.row.save.assert_called_once_with(update_fields=['current_version'])
        command._sync_generated_inputs.assert_called_once_with(
            git_hash=revision, binary_path='/tmp/new-simc', binary_revision=revision)

    def test_sync_inputs_only_rejects_unrelated_legacy_revision(self):
        self.assertFalse(Command._revision_matches_git_hash(
            '1205-01-deadbee', '62ababb127bef2a35f96357968d455dde7de7616'))

    def test_sync_inputs_only_does_not_promote_revision_when_publication_fails(self):
        revision = '62ababb127bef2a35f96357968d455dde7de7616'
        command = Command()
        command.platform = 'linux64'
        command.simc_source_dir = str(self.source)
        command.simc_build_dir = str(self.source / 'build-cli')
        command.simc_binary_path = '/tmp/new-simc'
        command.wow_build_override = ''
        command.row = SimpleNamespace(
            current_version='1205-01-62ababb', save=mock.Mock())
        command._set_status = mock.Mock()
        command._sync_generated_inputs = mock.Mock(side_effect=CommandError('publish failed'))

        with mock.patch.object(command, '_resolve_paths', return_value=(
                command.simc_source_dir, command.simc_build_dir, command.simc_binary_path)), \
                mock.patch.object(command, '_get_row', return_value=command.row), \
                mock.patch.object(command, '_get_git_hash', return_value=revision), \
                mock.patch('builtins.open', mock.mock_open()), \
                mock.patch('fcntl.flock'):
            with self.assertRaisesRegex(CommandError, 'publish failed'):
                command.handle(
                    check=False, sync_inputs_only=True, apply_patches=False,
                    no_pull=True, threads=1, wow_build='')

        self.assertEqual(command.row.current_version, '1205-01-62ababb')
        command.row.save.assert_not_called()

    def old_apl(self):
        return SimcApl.objects.create(
            name='old', class_name='warrior', spec='warrior_fury',
            content='actions=/old', source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True, is_active=True, is_selectable=True,
            sync_version='old-revision', validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(b'actions=/old').hexdigest(),
            validation_revision='old-revision',
            validation_game_build='old-build',
        )

    def write_corpus(self):
        (self.source / 'ActionPriorityLists' / 'default' / 'warrior_fury.simc').write_text(
            'actions=/bloodthirst\n', encoding='utf-8')
        (self.source / 'profiles' / 'MID1' / 'MID1_warrior_fury.simc').write_text(
            'warrior="Baseline"\nlevel=90\nspec=fury\nhead=id=1\nmain_hand=id=2\n', encoding='utf-8')

    @mock.patch.object(Command, '_run')
    def test_runtime_manifest_export_loads_mid1_actor_profiles(self, run):
        self.write_corpus()

        def export(cmd, **_kwargs):
            target = next(value.split('=', 1)[1] for value in cmd
                          if value.startswith('apl_metadata_export='))
            Path(target).write_text(json.dumps({
                'simc_revision': 'a' * 40,
                'game_build': '12.0.1.70000',
            }), encoding='utf-8')

        run.side_effect = export
        path = self.command._export_runtime_manifest('a' * 40, '12.0.1.70000')
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))

        cmd = run.call_args.args[0]
        self.assertIn(str(self.source / 'profiles' / 'MID1' /
                          'MID1_warrior_fury.simc'), cmd)
        export_arg = next(value for value in cmd
                          if value.startswith('apl_metadata_export='))
        self.assertEqual(path, export_arg.split('=', 1)[1])

    @mock.patch.object(Command, '_run')
    def test_runtime_manifest_export_adds_validation_only_profiles_for_missing_specs(self, run):
        self.write_corpus()
        SimcApl.objects.create(
            name='Restoration', spec='druid_restoration', class_name='druid',
            content='actions=/wrath', source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True, is_active=True, sync_version='a' * 40,
        )
        SimcContentTemplate.objects.create(
            name='Balance', spec='druid_balance', class_name='druid',
            content=('druid="Balance"\nlevel=90\nspec=balance\ntalents=OLD\n'
                     'head=id=1\nmain_hand=id=2\n'),
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            is_active=True, sync_version='a' * 40,
        )
        captured = {}

        def export(cmd, **_kwargs):
            generated = [Path(value) for value in cmd
                         if 'lmonitor-simc-manifest-profile-' in value]
            captured['generated'] = generated
            captured['content'] = generated[0].read_text(encoding='utf-8')
            target = next(value.split('=', 1)[1] for value in cmd
                          if value.startswith('apl_metadata_export='))
            Path(target).write_text(json.dumps({
                'simc_revision': 'a' * 40, 'game_build': '12.0.1.70000',
            }), encoding='utf-8')

        run.side_effect = export
        path = self.command._export_runtime_manifest('a' * 40, '12.0.1.70000')
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))

        self.assertIn('spec=restoration', captured['content'])
        self.assertNotIn('talents=', captured['content'])
        self.assertTrue(all(not item.exists() for item in captured['generated']))

    def test_system_apl_validation_uses_short_canonical_spec(self):
        apl = SimpleNamespace(
            spec='deathknight_blood', class_name='deathknight',
            content='actions=/auto_attack')
        baseline = SimpleNamespace(content=(
            'deathknight="Baseline"\nlevel=90\nspec=blood\nhead=id=1\nmain_hand=id=2\n'))
        result = {'structural_valid': True, 'authoritative_valid': True}
        with mock.patch(
                'botend.management.commands.update_simc_binary.SimcComposer.compose_validation_input',
                return_value='validation input') as compose, \
                mock.patch(
                    'botend.management.commands.update_simc_binary.validate_payload',
                    return_value=result):
            actual = self.command._validate_system_apl(
                apl, baseline, 'a' * 40, '/tmp/simc', 'a' * 40)
        self.assertEqual(actual, result)
        self.assertEqual(compose.call_args.args[0].spec, 'blood')
        self.assertEqual(compose.call_args.args[0].class_name, 'deathknight')

    def test_missing_exact_baseline_derives_validation_only_same_class_profile(self):
        source = SimpleNamespace(
            spec='druid_balance', class_name='druid', content=(
                'druid="Balance"\nlevel=90\nspec=balance\ntalents=OLD\n'
                'head=id=1\nmain_hand=id=2\n'))
        derived = self.command._validation_baseline_for_spec(
            'druid_restoration', {'druid_balance': source})
        self.assertIsNotNone(derived)
        self.assertIn('spec=restoration', derived.content)
        self.assertNotIn('talents=', derived.content)
        self.assertEqual(source.content.count('spec=balance'), 1)

    @mock.patch('botend.management.commands.update_simc_binary.call_command')
    @mock.patch.object(Command, '_export_runtime_manifest', return_value='/tmp/manifest.json')
    @mock.patch.object(Command, '_sync_default_template')
    @mock.patch.object(Command, '_get_git_hash', return_value='a' * 40)
    def test_complete_corpus_is_authoritatively_published(self, _git, _template, _manifest, calls):
        self.write_corpus()

        def dispatch(name, **kwargs):
            if name == 'import_simc_apl':
                from django.core.management import call_command
                return call_command(name, **kwargs)
            if name == 'import_simc_player_templates':
                SimcContentTemplate.objects.update_or_create(
                    template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
                    source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                    spec='warrior_fury',
                    defaults={'name': 'baseline', 'class_name': 'warrior',
                              'content': 'warrior="Baseline"\nspec=fury\n',
                              'sync_version': kwargs['sync_version'], 'is_active': True,
                              'is_selectable': False},
                )
                return None
            if name == 'sync_simc_apl_symbols':
                return None
        calls.side_effect = dispatch
        valid = {'structural_valid': True, 'authoritative_valid': True, 'diagnostics': []}
        with mock.patch.object(self.command, '_validate_system_apl', return_value=valid), \
                mock.patch('os.unlink'):
            self.command._sync_generated_inputs()

        apl = SimcApl.objects.get(spec='warrior_fury')
        self.assertEqual(apl.validation_status, SimcApl.VALIDATION_VALID)
        self.assertTrue(apl.is_selectable)
        self.assertEqual(apl.validation_revision, 'a' * 40)
        self.assertEqual(apl.validation_game_build, '12.0.1.70000')
        self.assertEqual(apl.sync_version, 'a' * 40)
        self.assertTrue(apl.validated_content_hash)

    @mock.patch('botend.management.commands.update_simc_binary.call_command')
    @mock.patch.object(Command, '_export_runtime_manifest', return_value='/tmp/manifest.json')
    @mock.patch.object(Command, '_sync_default_template')
    @mock.patch.object(Command, '_get_git_hash', return_value='b' * 40)
    def test_failed_authoritative_validation_preserves_old_release(self, _git, _template, _manifest, calls):
        old = self.old_apl()
        self.write_corpus()

        def dispatch(name, **kwargs):
            if name == 'import_simc_apl':
                from django.core.management import call_command
                return call_command(name, **kwargs)
            if name == 'import_simc_player_templates':
                SimcContentTemplate.objects.update_or_create(
                    template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
                    source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                    spec='warrior_fury',
                    defaults={'name': 'baseline', 'class_name': 'warrior',
                              'content': 'warrior="Baseline"\nspec=fury\n',
                              'sync_version': kwargs['sync_version'], 'is_active': True,
                              'is_selectable': False},
                )
                return None
            return None
        calls.side_effect = dispatch
        invalid = {'structural_valid': True, 'authoritative_valid': False,
                   'diagnostics': [{'message': 'bad action'}]}
        with mock.patch.object(self.command, '_validate_system_apl', return_value=invalid), \
                mock.patch('os.unlink'):
            with self.assertRaisesRegex(CommandError, '权威校验失败'):
                self.command._sync_generated_inputs()

        old.refresh_from_db()
        self.assertEqual(old.content, 'actions=/old')
        self.assertEqual(old.sync_version, 'old-revision')
        self.assertEqual(old.validation_status, SimcApl.VALIDATION_VALID)
        self.assertTrue(old.is_selectable)
        self.assertFalse(SimcContentTemplate.objects.filter(sync_version='b' * 40).exists())
