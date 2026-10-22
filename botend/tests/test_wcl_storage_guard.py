"""Real filesystem checks; no disk mocks and no database setup."""

import os
import shutil
import traceback
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from botend.services.wcl_storage_guard import (
    DEFAULT_MIN_FREE_DISK_BYTES,
    WCLStorageError,
    check_wcl_storage,
)


class WCLStorageGuardTests(SimpleTestCase):
    def test_real_disk_low_and_high_thresholds_are_checked_on_every_call(self):
        measured = shutil.disk_usage(settings.BASE_DIR)
        self.assertGreater(measured.free, 1, "Test requires some real free space")
        low = max(1, measured.free // 2)
        # Above total capacity, so unrelated concurrent deletions cannot unblock it.
        high = measured.total * 2
        with override_settings(WCL_MIN_FREE_DISK_BYTES=high):
            with self.assertRaises(WCLStorageError) as caught:
                check_wcl_storage()
        error = caught.exception
        self.assertEqual(error.reason, "insufficient_space")
        self.assertEqual(error.min_free_bytes, high)
        self.assertGreaterEqual(error.free_bytes, 0)
        self.assertLess(error.free_bytes, high)
        self.assertIn(f"free_bytes={error.free_bytes}", str(error))
        self.assertIn(f"min_free_bytes={high}", str(error))
        with override_settings(WCL_MIN_FREE_DISK_BYTES=low):
            status = check_wcl_storage()
        self.assertEqual(status.min_free_bytes, low)
        self.assertGreaterEqual(status.free_bytes, low)
        self.assertLessEqual(status.free_bytes, measured.total)

    def test_unconfigured_default_is_five_gib_and_never_disabled(self):
        self.assertEqual(DEFAULT_MIN_FREE_DISK_BYTES, 5 * 1024 ** 3)
        # Delete only this override, even if a developer has configured a value.
        with override_settings():
            if hasattr(settings, "WCL_MIN_FREE_DISK_BYTES"):
                del settings.WCL_MIN_FREE_DISK_BYTES
            try:
                status = check_wcl_storage()
            except WCLStorageError as error:
                self.assertEqual(error.reason, "insufficient_space")
                self.assertEqual(error.min_free_bytes, DEFAULT_MIN_FREE_DISK_BYTES)
                self.assertGreaterEqual(error.free_bytes, 0)
                self.assertLess(error.free_bytes, DEFAULT_MIN_FREE_DISK_BYTES)
            else:
                self.assertEqual(status.min_free_bytes, DEFAULT_MIN_FREE_DISK_BYTES)
                self.assertGreaterEqual(status.free_bytes, DEFAULT_MIN_FREE_DISK_BYTES)

    def test_invalid_configuration_fails_closed_without_echoing_value(self):
        secret = "credential-must-not-appear"
        for invalid in (None, 0, -1, True, False, 1.5, "123", secret, [], {}):
            with self.subTest(value_type=type(invalid).__name__):
                with override_settings(WCL_MIN_FREE_DISK_BYTES=invalid):
                    with self.assertRaises(WCLStorageError) as caught:
                        check_wcl_storage()
                error = caught.exception
                self.assertEqual(error.reason, "invalid_configuration")
                self.assertIsNone(error.free_bytes)
                self.assertIsNone(error.min_free_bytes)
                self.assertIn("free_bytes=unknown", str(error))
                self.assertIn("min_free_bytes=invalid", str(error))
                self.assertNotIn(secret, str(error))

    def test_unreadable_base_dir_fails_closed_and_sanitizes_traceback(self):
        secret = "credential-must-not-appear"
        missing = Path(settings.BASE_DIR) / f"{secret}-{uuid4().hex}" / "missing"
        self.assertFalse(missing.exists())
        for invalid_path in (missing, f"{secret}\0", None, 0):
            with self.subTest(path_type=type(invalid_path).__name__):
                with override_settings(BASE_DIR=invalid_path, WCL_MIN_FREE_DISK_BYTES=1):
                    with self.assertRaises(WCLStorageError) as caught:
                        check_wcl_storage()
                error = caught.exception
                self.assertEqual(error.reason, "check_failed")
                self.assertIsNone(error.free_bytes)
                self.assertEqual(error.min_free_bytes, 1)
                self.assertIn("free_bytes=unknown", str(error))
                self.assertIn("min_free_bytes=1", str(error))
                self.assertNotIn(secret, "".join(traceback.format_exception(error)))
        self.assertFalse(missing.exists())

    def test_base_dir_is_used_instead_of_current_working_directory(self):
        original_cwd = os.getcwd()
        # A real cwd change catches implementations that silently inspect '.'.
        # The unavailable BASE_DIR must fail even though cwd itself is readable.
        try:
            os.chdir("/")
            missing = Path(original_cwd) / f"wcl-guard-absent-{uuid4().hex}"
            with override_settings(BASE_DIR=missing, WCL_MIN_FREE_DISK_BYTES=1):
                with self.assertRaises(WCLStorageError) as caught:
                    check_wcl_storage()
            self.assertEqual(caught.exception.reason, "check_failed")
        finally:
            os.chdir(original_cwd)
