"""后台轮换：先停领长任务，长任务收尾期间保持短任务采集，再关闭全部入口。

USR1 请求计划轮换，INT/TERM 为显式停止。数据库续租不是运行期限；每个准入操作
保留独立有限期限（默认48小时）及清理宽限。异常退出后的恢复仍由原租约协议承担。
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
        self._retirement_requested = False
        self._lock = threading.RLock()
        self._active = {}
        self._rotation_holds = {}

    @property
    def retirement_requested(self):
        if self._retire_at is not None and time.monotonic() >= self._retire_at:
            self._retirement_requested = True
        return self._retirement_requested

    @property
    def retiring(self):
        with self._lock:
            if self.retirement_requested and not self._retiring:
                # 长任务收尾期间继续普通采集；所有长任务期限到达后仍进入有限排空。
                if not any(end > time.monotonic() for end in self._rotation_holds.values()):
                    self._retiring = True
            return self._retiring

    def request_retirement(self, signum=None, frame=None):
        # 信号处理仅置标志，不持锁；领取中途提出轮换时，已准入操作仍执行并清理。
        self._retirement_requested = True

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

    @contextmanager
    def monitor_admission(self):
        """领取前登记在途操作；轮换申请后只允许领取短任务。"""
        token = object()
        with self._lock:
            permit = None
            if not self.retiring:
                permit = _MonitorAdmission(self, token, not self.retirement_requested)
                deadline = time.monotonic() + self.max_runtime
                self._active[token] = ('monitor-claim-and-execution', deadline)
                if permit.allow_long_tasks:
                    # 领取事务尚未返回时不能确定任务类型，必须保护随后可能执行的长任务。
                    self._rotation_holds[token] = deadline
        try:
            yield permit
        finally:
            if permit is not None:
                with self._lock:
                    self._rotation_holds.pop(token, None)
                    self._active.pop(token, None)

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while not self.retiring and time.monotonic() < end:
            time.sleep(min(.1, max(0, end - time.monotonic())))
        return self.retiring

    def drain_threads(self, threads):
        self.request_retirement()
        # 主循环退出或异常均强制关闭入口；等待长任务时主循环尚未进入此处。
        self._retiring = True
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


class _MonitorAdmission:
    def __init__(self, lifecycle, token, allow_long_tasks):
        self.lifecycle = lifecycle
        self.token = token
        self.allow_long_tasks = allow_long_tasks

    def classify(self, *, is_long):
        """短任务和空领取不延迟轮换；已准入长任务保留到清理结束。"""
        if is_long and not self.allow_long_tasks:
            raise RuntimeError('轮换期间不得领取新长任务')
        if not is_long:
            with self.lifecycle._lock:
                self.lifecycle._rotation_holds.pop(self.token, None)


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
