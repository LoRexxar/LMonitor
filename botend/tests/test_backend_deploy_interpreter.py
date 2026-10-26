"""Execute the deploy-to-supervisor boundary without starting application work."""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import venv


class BackendDeployInterpreterTests(unittest.TestCase):
    def test_backend_uses_selected_project_python_without_exported_override(self):
        project = Path(__file__).resolve().parents[2]
        commands = [line for line in (project / 'deploy.sh').read_text().splitlines()
                    if line.startswith('screen -dmS lmback ')]
        self.assertEqual(len(commands), 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv.EnvBuilder(with_pip=False).create(root / '.venv')
            shutil.copy2(project / 'start.sh', root / 'start.sh')
            self.assertTrue(os.access(root / 'start.sh', os.X_OK))
            ready = root / 'backend-ready.json'
            (root / 'manage.py').write_text(
                'import json, os, sys, time\n'
                'from pathlib import Path\n'
                'Path(os.environ["BACKEND_PROBE_READY"]).write_text(json.dumps({\n'
                '    "prefix": sys.prefix, "argv": sys.argv[1:], "pid": os.getpid()}))\n'
                'time.sleep(60)\n'
            )
            env = dict(os.environ)
            env.pop('PYTHON_BIN', None)
            env.update(BACKEND_PROBE_READY=str(ready),
                       BACKEND_LOCK_FILE=str(root / 'backend.lock'),
                       STOP_GRACE_SECONDS='0.05', CHECK_INTERVAL='0.05')
            # Replace only screen's detach operation. Execute its actual login
            # shell and the unmodified supervisor, with a real isolated venv.
            shell = ('screen() { shift 2; "$@"; }; '
                     'PROJECT_DIR=' + shlex.quote(str(root)) + '; '
                     'PYTHON_BIN=.venv/bin/python; ' + commands[0])
            with (root / 'output.log').open('w+') as output:
                process = subprocess.Popen(['bash', '-c', shell], cwd=root, env=env,
                                           stdout=output, stderr=output,
                                           start_new_session=True)
                evidence = None
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if ready.exists():
                            try:
                                evidence = json.loads(ready.read_text())
                                break
                            except json.JSONDecodeError:
                                pass
                        if process.poll() is not None:
                            break
                        time.sleep(0.02)
                    output.flush(); output.seek(0)
                    if evidence is None:
                        self.fail(output.read())
                    self.assertEqual(evidence['argv'], ['LMonitorCoreBackend'])
                    self.assertEqual(evidence['prefix'], str(root / '.venv'))
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
                    if evidence:
                        try:
                            os.kill(evidence['pid'], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
