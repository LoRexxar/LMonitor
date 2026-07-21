
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from botend.models import SimcApl


class ImportSimcAplCommandTests(TestCase):
    def apl(self, spec, **overrides):
        values = dict(name=spec, class_name=spec.split('_', 1)[0], spec=spec,
                      content='actions=/old', source='simc_upstream', is_system=True,
                      is_selectable=True)
        values.update(overrides)
        return SimcApl.objects.create(**values)

    def test_sync_version_is_persisted_for_same_revision_consumers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'warrior_fury.simc').write_text('actions=/bloodthirst\n', encoding='utf-8')
            call_command('import_simc_apl', source_dir=tmpdir, sync_version='deadbeef', stdout=StringIO())
        self.assertEqual(SimcApl.objects.get().sync_version, 'deadbeef')

    def test_import_keeps_structurally_checked_state_unpublished(self):
        content = 'actions=/bloodthirst\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'warrior_fury.simc').write_text(content, encoding='utf-8')
            call_command('import_simc_apl', source_dir=tmpdir, sync_version='deadbeef',
                         stdout=StringIO())
        apl = SimcApl.objects.get()
        self.assertEqual(apl.validation_status, SimcApl.VALIDATION_DRAFT)
        self.assertEqual(apl.validated_content_hash, '')
        self.assertFalse(apl.is_selectable)

    def test_strict_missing_directory_fails(self):
        with self.assertRaisesRegex(CommandError, '目录不存在'):
            call_command('import_simc_apl', source_dir='/does/not/exist', strict=True,
                         stdout=StringIO())

    def test_strict_empty_or_no_canonical_apl_fails_without_writes(self):
        for filename, content in (('README.txt', 'notes'), ('warrior_fury.simc', '   '),
                                  ('invalid.simc', 'actions=/wait')):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmpdir:
                Path(tmpdir, filename).write_text(content, encoding='utf-8')
                with self.assertRaises(CommandError):
                    call_command('import_simc_apl', source_dir=tmpdir, strict=True,
                                 stdout=StringIO())
                self.assertEqual(SimcApl.objects.count(), 0)

    def test_strict_full_import_deactivates_only_missing_managed_upstream_rows(self):
        kept = self.apl('warrior_fury')
        missing = self.apl('warrior_arms')
        user = self.apl('warrior_protection', source='user', is_system=False,
                        owner_user_id=7)
        manual_system = self.apl('mage_fire', source='user', is_system=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'warrior_fury.simc').write_text(
                'actions=/bloodthirst\n', encoding='utf-8')
            call_command('import_simc_apl', source_dir=tmpdir, sync_version='new',
                         strict=True, stdout=StringIO())
        for row in (kept, missing, user, manual_system):
            row.refresh_from_db()
        self.assertTrue(kept.is_active)
        self.assertFalse(kept.is_selectable)
        self.assertEqual(kept.validation_status, SimcApl.VALIDATION_DRAFT)
        self.assertFalse(missing.is_active or missing.is_selectable)
        self.assertIsNone(missing.active_unique_key)
        self.assertTrue(user.is_active and user.is_selectable)
        self.assertTrue(manual_system.is_active and manual_system.is_selectable)

    def test_non_strict_partial_import_does_not_deactivate_missing_rows(self):
        missing = self.apl('warrior_arms')
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'warrior_fury.simc').write_text('actions=/new\n', encoding='utf-8')
            call_command('import_simc_apl', source_dir=tmpdir, stdout=StringIO())
        missing.refresh_from_db()
        self.assertTrue(missing.is_active and missing.is_selectable)

    def test_strict_invalid_expression_fails_before_updates_or_deactivation(self):
        old = self.apl('warrior_fury')
        missing = self.apl('warrior_arms')
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'warrior_fury.simc').write_text(
                'actions=/new,if=buff..foo.up\n', encoding='utf-8')
            with self.assertRaisesRegex(CommandError, '校验失败'):
                call_command('import_simc_apl', source_dir=tmpdir, strict=True,
                             stdout=StringIO())
        old.refresh_from_db()
        missing.refresh_from_db()
        self.assertEqual(old.content, 'actions=/old')
        self.assertTrue(missing.is_active and missing.is_selectable)

    def test_strict_stages_entire_corpus_before_any_orm_write(self):
        self.apl('warrior_arms')
        with tempfile.TemporaryDirectory() as tmpdir:
            # Sorted order deliberately places a valid file before an invalid one.
            Path(tmpdir, 'warrior_arms.simc').write_text(
                'actions=/mortal_strike\n', encoding='utf-8')
            Path(tmpdir, 'warrior_fury.simc').write_text(
                'actions=/bloodthirst,if=buff..foo.up\n', encoding='utf-8')
            with patch.object(SimcApl.objects, 'update_or_create') as upsert, \
                    patch.object(SimcApl, 'save') as save:
                with self.assertRaises(CommandError):
                    call_command('import_simc_apl', source_dir=tmpdir, strict=True,
                                 stdout=StringIO())

        upsert.assert_not_called()
        save.assert_not_called()
