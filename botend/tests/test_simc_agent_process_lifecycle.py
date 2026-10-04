"""Real child-process regression; no database or mocked execution.

POSIX SIGTERM resistance exercises the kill fallback on Linux. This does not
claim to reproduce Windows sharing violations or validate Windows APIs.
"""
import os
import signal
import subprocess
import sys
import unittest

from simc_agent_consumer import SimcAgentConsumer


class SimcAgentProcessLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'requires a SIGTERM-resistant child')
    def test_stop_process_reaps_killed_child_before_returning_to_workdir_cleanup(self):
        process = subprocess.Popen(
            [sys.executable, '-u', '-c',
             'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             'print("ready", flush=True); time.sleep(30)'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            # The readiness handshake guarantees terminate takes the kill path.
            self.assertEqual(process.stdout.readline(), b'ready\n')
            SimcAgentConsumer._stop_process(process)
            # Do not call poll()/wait() here: that would hide the missing reap.
            self.assertEqual(process.returncode, -signal.SIGKILL)
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
