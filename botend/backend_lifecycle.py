"""Process-local admission fence for planned backend retirement.

USR1 means retire, INT/TERM mean an explicit operator stop. A renewable DB lease
is NOT a runtime deadline. Each admitted operation has a separate finite runtime
contract (default 48h, configurable); retirement never kills an operation still
inside that contract. Python threads cannot safely be cancelled individually, so
if draining hangs we only exit after *all* admitted deadlines plus cleanup grace.
Lease recovery after that abnormal exit remains the existing owner/TTL protocol.
"""
from contextlib import contextmanager
import math
import os
import signal
import threading
import time


class BackendLifecycle:
    def __init__(self, *, max_runtime=172800, cleanup_seconds=60, retire_after=None):
        retire_after = float(os.environ.get('LMONITOR_BACKEND_RETIRE_AFTER', '0')
                             if retire_after is None else retire_after)
        if not math.isfinite(retire_after) or retire_after < 0:
            raise ValueError('backend retirement interval must be finite and nonnegative')
        self._retire_at = time.monotonic() + retire_after if retire_after else None
        self.max_runtime = float(max_runtime)
        self.cleanup_seconds = float(cleanup_seconds)
        if any(not math.isfinite(value) or value <= 0
               for value in (self.max_runtime, self.cleanup_seconds)):
            raise ValueError('backend runtime and cleanup limits must be finite and positive')
        self._retiring = False
        self._lock = threading.Lock()
        self._active = {}

    @property
    def retiring(self):
        if self._retire_at is not None and time.monotonic() >= self._retire_at:
            self._retiring = True
        return self._retiring

    def request_retirement(self, signum=None, frame=None):
        # Signal handlers must not acquire locks: the main thread may already
        # hold one. The gate's flag check is the admission linearization point;
        # a concurrent already-admitted claim is tracked until its cleanup ends.
        self._retiring = True

    def install_signal_handlers(self):
        previous = {}
        for signum, handler in ((signal.SIGUSR1, self.request_retirement),
                                (signal.SIGINT, self._operator_stop),
                                (signal.SIGTERM, self._operator_stop)):
            previous[signum] = signal.signal(signum, handler)
        return previous

    @staticmethod
    def _operator_stop(signum, frame):
        # Deliberately separate from planned retirement. Do not let non-daemon
        # scan threads hold an explicit operator stop hostage.
        os._exit(128 + signum)

    @contextmanager
    def admission(self, label):
        token = object()
        with self._lock:
            admitted = not self.retiring
            if admitted:
                self._active[token] = (str(label), time.monotonic() + self.max_runtime)
        try:
            yield admitted
        finally:
            if admitted:
                with self._lock:
                    del self._active[token]

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while not self.retiring and time.monotonic() < end:
            time.sleep(min(.1, max(0, end - time.monotonic())))
        return self.retiring

    def drain_threads(self, threads):
        self.request_retirement()
        with self._lock:
            # No admissions after this snapshot: flag + admission lock also
            # covers claims currently inside DB transactions (no lease TOCTOU).
            deadline = max([time.monotonic()] + [end for _, end in self._active.values()])
        deadline += self.cleanup_seconds
        print('[Backend] retiring: admission closed; draining workers', flush=True)
        while any(thread.is_alive() for thread in threads):
            if time.monotonic() >= deadline:
                with self._lock:
                    active = [label for label, _ in self._active.values()]
                print(f'[Backend] retirement deadline exceeded; active={active!r}', flush=True)
                os._exit(75)
            for thread in threads:
                thread.join(timeout=.05)
        print('[Backend] drained: all workers exited', flush=True)


def worker_cycles(lifecycle, *, interval, label):
    """Fence a whole maintenance/claim cycle, including cleanup, before sleeping.

    Callers without a backend lifecycle preserve their existing standalone
    behavior. No queued next cycle is consumed once the admission gate closes.
    """
    while True:
        if lifecycle is None:
            yield
            time.sleep(interval() if callable(interval) else interval)
        else:
            with lifecycle.admission(label) as admitted:
                if not admitted:
                    return
                yield
            if lifecycle.wait(interval() if callable(interval) else interval):
                return
