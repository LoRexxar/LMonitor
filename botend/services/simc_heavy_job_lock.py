import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path


SIMC_HEAVY_JOB_LOCK_PATH = Path('/tmp/lmonitor-simc-update.lock')


class SimcHeavyJobLockBusy(RuntimeError):
    pass


@contextmanager
def acquire_simc_heavy_job_lock():
    """Acquire the host-wide lock shared by SimC compile and snapshot jobs."""
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    lock_fd = os.open(SIMC_HEAVY_JOB_LOCK_PATH, lock_flags, 0o600)
    with os.fdopen(lock_fd, 'w') as lock_file:
        lock_stat = os.fstat(lock_file.fileno())
        if lock_stat.st_uid != os.getuid() or not stat.S_ISREG(lock_stat.st_mode):
            raise RuntimeError('SimC 重型作业锁文件不安全')
        os.fchmod(lock_file.fileno(), 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SimcHeavyJobLockBusy from exc
        yield
