"""Read-only, fail-closed disk admission check for WCL writers.

This checks the filesystem containing settings.BASE_DIR, not remote database or
object storage capacity. Callers own scheduling, deferral and transaction policy;
passing a check is an observation, not a reservation of disk space.
"""

import os
import shutil
from dataclasses import dataclass

from django.conf import settings


DEFAULT_MIN_FREE_DISK_BYTES = 5 * 1024 ** 3


@dataclass(frozen=True)
class WCLStorageStatus:
    free_bytes: int
    min_free_bytes: int


class WCLStorageError(RuntimeError):
    """Safe diagnostic for callers to record without exposing settings/paths."""

    def __init__(self, reason: str, *, free_bytes: int | None, min_free_bytes: int | None):
        self.reason = reason
        self.free_bytes = free_bytes
        self.min_free_bytes = min_free_bytes
        free = "unknown" if free_bytes is None else str(free_bytes)
        minimum = "invalid" if min_free_bytes is None else str(min_free_bytes)
        super().__init__(
            f"WCL storage guard: {reason}; free_bytes={free}; min_free_bytes={minimum}"
        )


def check_wcl_storage() -> WCLStorageStatus:
    """Return measured capacity or raise WCLStorageError; never alter state.

    WCL_MIN_FREE_DISK_BYTES defaults to 5 GiB when absent. An explicit value must
    be a positive Python int (not bool, float, string or None); invalid values
    block writes rather than disabling the guard. Settings and capacity are read
    on every call. Exactly meeting the threshold is sufficient.

    Only safe numeric diagnostics are exposed. Unavailable capacity is unknown,
    not zero; neither BASE_DIR nor an underlying OS exception is logged/chained.
    There is no database/network access, cleanup, waiting or automatic recovery.
    """
    minimum = getattr(settings, "WCL_MIN_FREE_DISK_BYTES", DEFAULT_MIN_FREE_DISK_BYTES)
    if type(minimum) is not int or minimum <= 0:
        raise WCLStorageError(
            "invalid_configuration", free_bytes=None, min_free_bytes=None
        )

    try:
        # fspath rejects file descriptors: the guard must inspect BASE_DIR's FS.
        free = shutil.disk_usage(os.fspath(settings.BASE_DIR)).free
    except (OSError, TypeError, ValueError, AttributeError):
        # OS error text can contain paths/credentials. Do not expose its chain.
        raise WCLStorageError(
            "check_failed", free_bytes=None, min_free_bytes=minimum
        ) from None

    if free < minimum:
        raise WCLStorageError(
            "insufficient_space", free_bytes=free, min_free_bytes=minimum
        )
    return WCLStorageStatus(free_bytes=free, min_free_bytes=minimum)
