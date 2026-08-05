#!/bin/bash
set -e

exec 9>/tmp/lmonitor-deploy.lock
flock -n 9 || { echo "另一个部署正在运行"; exit 1; }

kill_processes() {
    local pattern="$1"
    local pids
    pids="$(pgrep -f "$pattern" || true)"
    if [ -n "$pids" ]; then
        kill -INT $pids 2>/dev/null || true
        sleep 3
        pids="$(pgrep -f "$pattern" || true)"
        if [ -n "$pids" ]; then
            kill -KILL $pids 2>/dev/null || true
        fi
    fi
}

PYTHON_BIN="${PYTHON_BIN:-python3}"
WOW_BUILD="12.0.7.68453"
if [ -x .venv/bin/python ]; then
    PYTHON_BIN=".venv/bin/python"
fi

echo "=== 1. Git pull ==="
GIT_MERGE_AUTOEDIT=no git pull origin master

echo "=== 2. Migrate ==="

"$PYTHON_BIN" manage.py migrate --no-input

echo "=== 3. 安装并刷新 PTR 12.1 天赋 DB2 数据 ==="
PTR_DB2_BUILD="12.1.0.68914"
PTR_DB2_ASSET="botend/data/ptr_talent_db2_${PTR_DB2_BUILD}.tar.gz"
PTR_DB2_DIR=".cache/wago_db2_dumps/${PTR_DB2_BUILD}"
PTR_DB2_TMP="${PTR_DB2_DIR}.tmp.$$"
PTR_DB2_PREVIOUS="${PTR_DB2_TMP}"
PTR_DB2_HAD_OLD=0
PTR_DB2_OLD_ID=""
PTR_DB2_REPAIR_SUCCEEDED=0
PTR_DB2_INSTALL_STATE="preparing"
PTR_DB2_CLEANUP_RUNNING=0
PTR_DB2_CLEANUP_DONE=0

if [ -e "$PTR_DB2_DIR" ] && [ ! -d "$PTR_DB2_DIR" ]; then
    echo "PTR DB2 canonical path 不是目录: $PTR_DB2_DIR"
    exit 1
fi
if [ -d "$PTR_DB2_DIR" ]; then
    PTR_DB2_HAD_OLD=1
    PTR_DB2_OLD_ID="$(stat -Lc '%d:%i' "$PTR_DB2_DIR")"
fi

cleanup_ptr_db2_install() {
    [ "$PTR_DB2_CLEANUP_DONE" = "0" ] || return 0
    [ "$PTR_DB2_CLEANUP_RUNNING" = "0" ] || return 0
    PTR_DB2_CLEANUP_RUNNING=1
    if ! "$PYTHON_BIN" - \
        "$PTR_DB2_DIR" "$PTR_DB2_TMP" "$PTR_DB2_HAD_OLD" \
        "$PTR_DB2_OLD_ID" "$PTR_DB2_REPAIR_SUCCEEDED" <<'PY'
import ctypes
import os
import shutil
import sys
from pathlib import Path

canonical = Path(sys.argv[1])
rollback = Path(sys.argv[2])
had_old = sys.argv[3] == '1'
old_id = tuple(int(part) for part in sys.argv[4].split(':')) if sys.argv[4] else None
repair_succeeded = sys.argv[5] == '1'


def identity(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def exchange(left, right):
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(
        ctypes.c_int(-100), os.fsencode(left),
        ctypes.c_int(-100), os.fsencode(right),
        ctypes.c_uint(2),  # RENAME_EXCHANGE
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f'{left} <-> {right}')


def remove_rollback():
    if rollback.exists():
        shutil.rmtree(rollback)


if repair_succeeded:
    # The old directory remains rollback data until repair has committed.
    remove_rollback()
elif had_old:
    canonical_id = identity(canonical)
    rollback_id = identity(rollback)
    if canonical_id == old_id:
        # Exchange never happened, or a prior cleanup already restored it.
        remove_rollback()
    elif rollback_id == old_id:
        if canonical.exists():
            exchange(canonical, rollback)
            remove_rollback()
        else:
            # Defensive recovery: preserve the invariant even if an external
            # actor removed the canonical directory.
            os.replace(rollback, canonical)
    else:
        raise RuntimeError(
            'cannot identify prior PTR DB2 directory; refusing destructive cleanup'
        )
elif rollback.exists():
    # Before a first install this is only validated staging data. After
    # os.replace(), rollback is absent and the canonical cache stays installed
    # even when repair fails, so the canonical path is never removed.
    remove_rollback()
PY
    then
        echo "PTR DB2 清理/回滚失败；保留现有目录供人工恢复" >&2
        PTR_DB2_CLEANUP_RUNNING=0
        return 1
    fi
    PTR_DB2_CLEANUP_DONE=1
    PTR_DB2_CLEANUP_RUNNING=0
}

ptr_db2_exit_handler() {
    local status=$?
    trap - EXIT INT TERM
    cleanup_ptr_db2_install || status=1
    exit "$status"
}
trap ptr_db2_exit_handler EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[ -f "$PTR_DB2_ASSET" ] || { echo "PTR DB2 资产不存在: $PTR_DB2_ASSET"; exit 1; }
rm -rf "$PTR_DB2_TMP"
mkdir -p "$PTR_DB2_TMP" "$(dirname "$PTR_DB2_DIR")" .cache/backups
"$PYTHON_BIN" - "$PTR_DB2_ASSET" "$PTR_DB2_TMP" "$PTR_DB2_BUILD" <<'PY'
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

archive_path = Path(sys.argv[1])
root = Path(sys.argv[2])
expected_build = sys.argv[3]

with tarfile.open(archive_path, 'r:gz') as archive:
    members = archive.getmembers()
    by_name = {}
    for member in members:
        name = member.name
        path = PurePosixPath(name)
        if (
            not name
            or '\\' in name
            or path.is_absolute()
            or '..' in path.parts
            or '.' in path.parts
        ):
            raise SystemExit(f"PTR DB2 包含不安全路径: {name!r}")
        if name in by_name:
            raise SystemExit(f"PTR DB2 包含重复成员: {name}")
        if not member.isreg():
            raise SystemExit(f"PTR DB2 包含非普通文件成员: {name}")
        by_name[name] = member

    manifest_member = by_name.get('manifest.json')
    if manifest_member is None:
        raise SystemExit('PTR DB2 缺少 manifest.json')
    manifest_stream = archive.extractfile(manifest_member)
    if manifest_stream is None:
        raise SystemExit('PTR DB2 manifest.json 无法读取')
    try:
        manifest = json.loads(manifest_stream.read().decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f'PTR DB2 manifest.json 无效: {exc}') from exc

    if manifest.get('build') != expected_build:
        raise SystemExit(f"PTR DB2 build 不匹配: {manifest.get('build')} != {expected_build}")
    if manifest.get('version_key') != 'ptr-12.1.0':
        raise SystemExit(f"PTR DB2 version_key 不匹配: {manifest.get('version_key')}")
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise SystemExit('PTR DB2 manifest files 必须是非空对象')
    if set(by_name) != set(files) | {'manifest.json'}:
        raise SystemExit('PTR DB2 成员集合与 manifest 不完全一致')

    verified = {}
    for relative_path, expected in files.items():
        if not isinstance(relative_path, str) or not isinstance(expected, dict):
            raise SystemExit('PTR DB2 manifest 文件记录无效')
        expected_bytes = expected.get('bytes')
        expected_sha = expected.get('sha256')
        if (
            not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or not isinstance(expected_sha, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha)
        ):
            raise SystemExit(f"PTR DB2 manifest 完整性字段无效: {relative_path}")
        member = by_name.get(relative_path)
        if member is None or member.size != expected_bytes:
            raise SystemExit(f"PTR DB2 文件大小不匹配: {relative_path}")
        stream = archive.extractfile(member)
        if stream is None:
            raise SystemExit(f"PTR DB2 文件无法读取: {relative_path}")
        payload = stream.read()
        if len(payload) != expected_bytes:
            raise SystemExit(f"PTR DB2 文件大小不匹配: {relative_path}")
        if hashlib.sha256(payload).hexdigest() != expected_sha:
            raise SystemExit(f"PTR DB2 SHA256 不匹配: {relative_path}")
        verified[relative_path] = payload

    # Extraction happens only after every archive member and payload has been
    # validated. Files are written explicitly; tar paths are never trusted.
    manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    for relative_path, payload in {**verified, 'manifest.json': manifest_payload}.items():
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as handle:
            handle.write(payload)

print(f"PTR DB2 资产校验通过: {expected_build}, {len(files)} files")
PY
if [ "$PTR_DB2_HAD_OLD" = "1" ]; then
    "$PYTHON_BIN" - "$PTR_DB2_DIR" "$PTR_DB2_TMP" <<'PY'
import ctypes
import os
import sys

left, right = sys.argv[1:3]
libc = ctypes.CDLL(None, use_errno=True)
result = libc.renameat2(
    ctypes.c_int(-100), os.fsencode(left),
    ctypes.c_int(-100), os.fsencode(right),
    ctypes.c_uint(2),  # RENAME_EXCHANGE
)
if result != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error), f'{left} <-> {right}')
PY
    PTR_DB2_INSTALL_STATE="exchanged"
else
    "$PYTHON_BIN" - "$PTR_DB2_TMP" "$PTR_DB2_DIR" <<'PY'
import os
import sys

# First install has no rollback directory to exchange. os.replace is atomic
# and makes the validated staging directory canonical in one namespace step.
os.replace(sys.argv[1], sys.argv[2])
PY
    PTR_DB2_INSTALL_STATE="first-installed"
fi
if ! "$PYTHON_BIN" manage.py repair_ptr_talent_metadata \
    --version-key ptr-12.1.0 \
    --dump-dir "$PTR_DB2_DIR" \
    --backup-dir .cache/backups \
    --skip-wowhead
then
    echo "PTR 天赋刷新失败，恢复上一份 DB2 数据"
    exit 1
fi
PTR_DB2_REPAIR_SUCCEEDED=1
PTR_DB2_INSTALL_STATE="repair-succeeded"
# Only now may the old directory produced by RENAME_EXCHANGE be discarded.
rm -rf "$PTR_DB2_PREVIOUS"
PTR_DB2_INSTALL_STATE="committed"

echo "=== 4. 初始化/更新大秘境规划器数据 ==="
if [ "${IMPORT_MDT_DATA:-0}" = "1" ]; then
    "$PYTHON_BIN" manage.py import_mythic_dungeon_data --activate --replace
elif ! "$PYTHON_BIN" manage.py shell -c \
    "from botend.models import MythicDungeonDataVersion; raise SystemExit(0 if MythicDungeonDataVersion.objects.filter(is_active=True).exists() else 1)"
then
    "$PYTHON_BIN" manage.py import_mythic_dungeon_data --activate
else
    echo "已存在活动的大秘境数据版本，跳过导入。"
fi

echo "=== 5. Collectstatic ==="
"$PYTHON_BIN" manage.py collectstatic --no-input --ignore='simc_results/*'

echo "=== 6. 应用 SimC 本地补丁 ==="
"$PYTHON_BIN" manage.py update_simc_binary --apply-patches --threads 2 --wow-build "$WOW_BUILD"

echo "=== 7. 重启 lmweb ==="
screen -S lmweb -X quit 2>/dev/null || true
kill_processes 'manage.py runserver 0.0.0.0:18000'
sleep 2
screen -dmS lmweb bash -lc "cd ~/LMonitor && $PYTHON_BIN manage.py runserver 0.0.0.0:18000 --noreload"

echo "=== 8. 重启 lmback ==="
screen -S lmback -X quit 2>/dev/null || true
kill_processes 'LMonitorCoreBackend'
sleep 2
screen -dmS lmback bash -lc 'cd ~/LMonitor && ./start.sh'

echo "=== 9. 重启 lmsimc ==="
screen -S lmsimc -X quit 2>/dev/null || true
kill_processes 'manage.py simc_worker'
sleep 2
screen -dmS lmsimc bash -lc "cd ~/LMonitor && $PYTHON_BIN manage.py simc_worker"

echo "=== 10. 检查 screen 会话 ==="
for session in lmweb lmback lmsimc; do
    screen -list | grep -q "\.${session}" || {
        echo "screen 会话 ${session} 启动失败"
        exit 1
    }
done

web_ready=0
for _ in $(seq 1 15); do
    if curl -fsS http://127.0.0.1:18000/ >/dev/null; then
        web_ready=1
        break
    fi
    sleep 1
done
[ "$web_ready" = "1" ] || { echo "lmweb HTTP 健康检查失败"; exit 1; }
screen -list | grep -E '\.(lmweb|lmback|lmsimc)'

echo "=== 部署完成 ==="
