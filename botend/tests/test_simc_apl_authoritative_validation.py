import os
import stat
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from botend.services.simc_apl.authoritative_validator import (
    RestrictedSimcValidator, ValidatorLimits,
)
from botend.services.simc_apl.validation import validate_payload


class RestrictedAuthoritativeValidationTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def executable(self, body):
        path = Path(self.root.name) / 'simc'
        path.write_text('#!/bin/sh\n' + body, encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def validator(self, body='exit 0\n', **kwargs):
        return RestrictedSimcValidator(
            self.executable(body), temp_root=self.root.name,
            catalog_revision='rev', binary_revision='rev', **kwargs)

    def test_rejects_profile_level_option_and_include_without_starting_simc(self):
        runner = mock.Mock()
        validator = RestrictedSimcValidator('/does/not/matter', runner=runner)
        for source in ('option=foo\nactions=/x', '  include=../../secret'):
            with self.subTest(source=source):
                result = validator.validate(source)
                self.assertEqual(result['authoritative_error']['code'],
                                 'profile_directive_forbidden')
        runner.assert_not_called()

    def test_invokes_bounded_strict_parse_command_and_cleans_directory(self):
        validator = self.validator(
            'test "$2" = strict_parsing=1 || exit 2\n'
            'test "$3" = iterations=1 || exit 3\n'
            'test "$4" = threads=1 || exit 4\n'
            'test "$5" = fixed_time=1 || exit 5\n'
            'test "$6" = vary_combat_length=0 || exit 6\nexit 0\n')
        result = validator.validate('actions=/auto_attack')
        self.assertTrue(result['authoritative_valid'])
        self.assertEqual(list(Path(self.root.name).glob('apl-validation-*')), [])

    def test_revision_mismatch_is_stale_without_running_binary(self):
        validator = self.validator()
        result = validator.validate('actions=/x', validation_context={
            'catalog_revision': 'new', 'binary_revision': 'old'})
        self.assertEqual(result['authoritative_error']['code'], 'stale_binary')

    def test_timeout_kills_process_group_and_cleans_temp_file(self):
        validator = self.validator('sleep 30\n', limits=ValidatorLimits(timeout_seconds=.05))
        result = validator.validate('actions=/x')
        self.assertEqual(result['authoritative_error']['code'], 'timeout')
        self.assertEqual(list(Path(self.root.name).glob('apl-validation-*')), [])

    def test_stdout_and_stderr_are_bounded(self):
        validator = self.validator(
            "python3 -c 'import sys;sys.stdout.write(\"x\"*100);sys.stderr.write(\"y\"*100)'\n",
            limits=ValidatorLimits(max_output_bytes=32))
        result = validator.validate('actions=/x')
        self.assertEqual(result['authoritative_error']['code'], 'output_too_large')

    def test_fast_exit_output_is_still_bounded(self):
        validator = self.validator(
            "python3 -c 'print(\"x\"*10000)'\n",
            limits=ValidatorLimits(max_output_bytes=32))
        result = validator.validate('actions=/x')
        self.assertEqual(result['authoritative_error']['code'], 'output_too_large')

    def test_composed_validation_input_is_bounded_before_runner(self):
        runner = mock.Mock()
        validator = RestrictedSimcValidator(
            self.executable('exit 0\n'), temp_root=self.root.name, runner=runner,
            limits=ValidatorLimits(max_source_bytes=32))
        result = validator.validate(
            'actions=/x', validation_context={'validation_input': 'x' * 33})
        self.assertEqual(result['authoritative_error']['code'], 'validation_input_too_large')
        runner.assert_not_called()

    def test_server_composed_input_may_contain_profile_directives(self):
        validator = self.validator()
        result = validator.validate(
            'actions=/x', validation_context={
                'validation_input': 'warrior="Validation"\nspec=fury\niterations=1\nactions=/x',
            })
        self.assertTrue(result['authoritative_valid'])

    def test_missing_unique_profile_context_returns_structural_only(self):
        result = validate_payload('actions=/auto_attack', mode='both')
        self.assertTrue(result['structural_valid'])
        self.assertIsNone(result['authoritative_valid'])
        self.assertEqual(result['authoritative_status'], 'structural_only')

    def test_does_not_import_or_create_run_or_artifact_models(self):
        validator = self.validator()
        with mock.patch('botend.models.SimcTaskArtifact.objects.create') as artifact_create:
            validator.validate('actions=/auto_attack')
        artifact_create.assert_not_called()
