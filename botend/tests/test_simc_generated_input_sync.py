import hashlib
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
