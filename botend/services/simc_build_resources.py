"""共享主机优先使用编译作用域，环境不支持时使用普通用户可用的兼容保护。"""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid


MIB = 1024 ** 2
GIB = 1024 ** 3
BUILD_TIMEOUT = 10800


class BuildResourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildBudget:
    memory: int
    cpu_percent: int
    warnings: tuple = ()

    @property
    def high(self):
        return self.memory * 3 // 4


def choose_budget(total, available, cpus):
    # 不按瞬时可用内存缩小预算或拦截启动，避免缓存和业务波动导致无法编译。
    memory = max(MIB, min(4 * GIB, total // 2) // MIB * MIB)
    warnings = ('当前可用内存低于编译预算，编译可能受内存压力影响',) if available < memory else ()
    return BuildBudget(memory, min(100, max(1, cpus) * 50), warnings)


def constrain_memory(total, available, group, root):
    """同时预留调用进程所在容器或用户 slice 的内存，不能只看宿主机。"""
    group, root = group.resolve(), root.resolve()
    if not group.is_relative_to(root):
        raise ValueError('cgroup 路径越界')
    while group != root:
        try:
            maximum = (group / 'memory.max').read_text().strip()
        except FileNotFoundError:
            group = group.parent
            continue
        if maximum != 'max':
            limit = int(maximum)
            current = int((group / 'memory.current').read_text().strip())
            total, available = min(total, limit), min(available, max(0, limit - current))
        group = group.parent
    return total, available


def read_budget():
    if not sys.platform.startswith('linux'):
        raise BuildResourceError('服务器 SimC 编译需要 Linux')
    warnings = []
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            values[key] = int(value.split()[0]) * 1024
        total, available = values['MemTotal'], values['MemAvailable']
    except (OSError, ValueError, KeyError):
        total = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
        available = total
        warnings.append('无法读取当前可用内存，按物理内存设置预算，不阻止启动')
    try:
        root = Path('/sys/fs/cgroup')
        relative = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        total, available = constrain_memory(total, available, root / relative.lstrip('/'), root)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        warnings.append(f'未取得父级 cgroup 限额，继续按主机预算编译：{exc}')
    budget = choose_budget(total, available, os.cpu_count() or 1)
    return BuildBudget(budget.memory, budget.cpu_percent, tuple(warnings) + budget.warnings)


def helper_command(budget, source, build, entry_file, unit=None):
    command = [sys.executable, str(Path(__file__).resolve()), '--entry-file', str(entry_file),
               '--memory', str(budget.memory), '--cpu', str(budget.cpu_percent),
               '--source', str(source), '--build', str(build)]
    if unit:
        command.extend(['--unit', unit])
    return command


def scope_command(budget, source, build, unit, systemd_run, entry_file):
    command = [systemd_run, '--scope', '--quiet', '--no-ask-password', '--collect', f'--unit={unit}']
    if os.geteuid() != 0:
        command.append('--user')
    properties = {
        'MemoryMax': budget.memory, 'MemoryHigh': budget.high, 'MemorySwapMax': 0,
        'CPUQuota': f'{budget.cpu_percent}%', 'CPUWeight': 10, 'IOWeight': 10,
        'TasksMax': 64, 'RuntimeMaxSec': BUILD_TIMEOUT, 'TimeoutStopSec': 10,
        'KillMode': 'control-group',
    }
    command.extend(f'--property={key}={value}' for key, value in properties.items())
    command.extend(['--'] + helper_command(budget, source, build, entry_file, unit))
    return command


def verify_scope(budget, unit, root=Path('/sys/fs/cgroup'), membership=Path('/proc/self/cgroup')):
    """读取内核实际值，防止用户级 systemd 未委派控制器却看似启动成功。"""
    try:
        relative = next(line[3:] for line in membership.read_text().splitlines() if line.startswith('0::'))
        group = (root / relative.lstrip('/')).resolve()
        if not group.is_relative_to(root.resolve()) or group.name != unit:
            raise ValueError('进程不在指定编译作用域内')
        limit = int((group / 'memory.max').read_text().strip())
        high = int((group / 'memory.high').read_text().strip())
        swap = int((group / 'memory.swap.max').read_text().strip())
        quota, period = map(int, (group / 'cpu.max').read_text().split())
        tasks = int((group / 'pids.max').read_text().strip())
        if not (0 < limit <= budget.memory and 0 < high <= budget.high and swap == 0
                and period > 0 and 0 < quota * 100 <= period * budget.cpu_percent and 0 < tasks <= 64):
            raise ValueError('内核资源限制未达到编译预算要求')
        return group
    except (OSError, ValueError, StopIteration) as exc:
        raise BuildResourceError(f'编译资源隔离校验失败，未启动编译器：{exc}') from exc


def build_commands(source, build):
    return [
        ['cmake', '-S', str(source), '-B', str(build), '-DBUILD_GUI=OFF', '-DBUILD_TESTING=OFF',
         '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG',
         '-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF', '-G', 'Ninja'],
        ['ninja', '-C', str(build), '-j1', 'simc'],
    ]


def apply_compatible_limits(budget):
    # 只在独立构建进程内设置，不能改变 Web 进程的限制；子进程继承该上限。
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limits = [budget.memory] + [value for value in (soft, hard) if value != resource.RLIM_INFINITY]
    memory = min(limits)
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    affinity = False
    try:
        allowed = os.sched_getaffinity(0)
        os.sched_setaffinity(0, {min(allowed)})
        affinity = True
    except (AttributeError, OSError, ValueError):
        pass
    print(f'兼容保护：每个进程虚拟地址空间上限 {memory // MIB}MB，'
          f'{"绑定 1 核" if affinity else "单任务编译"}；此模式不提供进程树总内存或 swap 硬上限', flush=True)


def execute_inside_scope(budget, unit, source, build):
    group = None
    if unit:
        try:
            group = verify_scope(budget, unit)
        except BuildResourceError as exc:
            print(f'{exc}；改用兼容保护', flush=True)
    if group is None:
        apply_compatible_limits(budget)
    else:
        print(f'作用域限制已核验：内存 {budget.memory // MIB}MB，CPU {budget.cpu_percent}%，无 swap', flush=True)
    # 独立子进程持有锁，Web 父进程重启后也不能再启动第二份实际编译。
    import fcntl
    with open(Path(build) / '.lmonitor-compile.lock', 'a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BuildResourceError('已有隔离编译仍在运行，拒绝重复编译') from exc
        try:
            os.nice(10)
        except OSError:
            print('无法降低调度优先级，继续使用已设置的编译限制', flush=True)
        try:
            for command in build_commands(source, build):
                print('阶段：' + command[0], flush=True)
                result = subprocess.run(command, check=False, pass_fds=(lock.fileno(),))
                if result.returncode:
                    raise BuildResourceError(f'{command[0]} 失败，退出码 {result.returncode}；检查编译日志及内存限制事件')
        finally:
            for filename in ('memory.peak', 'memory.events'):
                try:
                    if group is not None:
                        print(filename + ': ' + (group / filename).read_text().strip(), flush=True)
                except OSError:
                    pass


def run_build(source, build, log_dir, budget):
    systemd_run, systemctl = shutil.which('systemd-run'), shutil.which('systemctl')
    unit = f'lmonitor-simc-build-{uuid.uuid4().hex}.scope'
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'{unit}.log'
    entry_file = log_dir / f'{unit}.entered'
    control = [systemctl] + (['--user'] if os.geteuid() != 0 else [])
    failure = None
    # 编译器输出直接写文件，避免 Django 进程 capture_output 累积整份日志。
    with log_path.open('w+b') as log:
        for warning in budget.warnings:
            log.write((warning + '\n').encode('utf-8'))
        log.flush()
        if not systemd_run or not systemctl:
            run_compatible_build(budget, source, build, entry_file, log)
            return log_path
        command = scope_command(budget, source, build, unit, systemd_run, entry_file)
        allow_fallback = False
        try:
            result = subprocess.run(command, cwd=source, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=BUILD_TIMEOUT + 30)
            if result.returncode:
                failure = f'隔离编译失败（退出码 {result.returncode}）'
                allow_fallback = not entry_file.exists()
        except subprocess.TimeoutExpired:
            failure = '隔离编译超过 3 小时，已请求终止整个编译作用域'
        except OSError as exc:
            failure = f'无法启动隔离编译：{exc}'
            allow_fallback = not entry_file.exists()
        finally:
            # 只清理本次 UUID 作用域；包括编译器、链接器及异常遗留子进程。
            try:
                stopped = subprocess.run(control + ['--no-ask-password', 'stop', unit], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=30, check=False)
                if stopped.returncode:
                    state = subprocess.run(control + ['show', '--property=LoadState', '--value', unit],
                                           capture_output=True, text=True, timeout=10, check=False)
                    if state.stdout.strip() != 'not-found':
                        failure = (failure or '编译作用域清理失败') + '；无法确认子进程已退出，需检查该作用域'
            except (OSError, subprocess.TimeoutExpired):
                failure = (failure or '编译作用域清理失败') + '；无法确认子进程已退出，需检查该作用域'
        if failure and allow_fallback:
            # 同步 systemd-run 已退出且辅助进程从未进入，不因用户管理器不可连接而阻断回退。
            log.seek(0, os.SEEK_END)
            log.write('systemd 未能启动构建进程，改用兼容保护继续编译\n'.encode('utf-8'))
            log.flush()
            run_compatible_build(budget, source, build, entry_file, log)
            failure = None
        if failure:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 2500))
            tail = log.read().decode('utf-8', errors='replace')
            raise BuildResourceError(f'{failure}。日志：{log_path}\n{tail}')
    return log_path


def run_compatible_build(budget, source, build, entry_file, log):
    command = helper_command(budget, source, build, entry_file)
    process = subprocess.Popen(command, cwd=source, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    try:
        code = process.wait(timeout=BUILD_TIMEOUT)
        if code:
            raise BuildResourceError(f'兼容保护模式编译失败（退出码 {code}），日志：{log.name}')
    except subprocess.TimeoutExpired as exc:
        raise BuildResourceError(f'兼容保护模式编译超过 3 小时，日志：{log.name}') from exc
    finally:
        # 会话由本次 Popen 创建，只终止这一组编译进程。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='在已限制资源的作用域中执行 SimC 编译')
    parser.add_argument('--unit')
    parser.add_argument('--entry-file', required=True)
    parser.add_argument('--memory', type=int, required=True)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--build', required=True)
    args = parser.parse_args()
    try:
        # 在任何资源校验、编译锁或编译命令之前写入，避免真实编译失败后再次回退执行。
        Path(args.entry_file).write_text('构建子进程已启动', encoding='utf-8')
        execute_inside_scope(BuildBudget(args.memory, args.cpu), args.unit, args.source, args.build)
    except (BuildResourceError, OSError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
