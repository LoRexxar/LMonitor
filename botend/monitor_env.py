import os


def is_windows():
    return os.name == "nt"


def is_task_runnable(task):
    try:
        env_limit = int(getattr(task, "env_limit", 0) or 0)
    except Exception:
        env_limit = 0
    if env_limit == 0:
        return True
    if is_windows():
        return env_limit != 2
    return env_limit != 1


def filter_runnable_tasks(qs):
    if is_windows():
        return qs.exclude(env_limit=2)
    return qs.exclude(env_limit=1)


def env_limit_hint(env_limit):
    try:
        env_limit = int(env_limit or 0)
    except Exception:
        env_limit = 0
    cur = "Windows" if is_windows() else "非 Windows"
    if env_limit == 1:
        if is_windows():
            return "该任务限制在 Windows 环境下执行（当前：Windows）"
        return "该任务限制在 Windows 环境下执行（当前：非 Windows，已拦截）"
    if env_limit == 2:
        if is_windows():
            return "该任务限制在非 Windows 环境下执行（当前：Windows，已拦截）"
        return "该任务限制在非 Windows 环境下执行（当前：非 Windows）"
    return f"该任务不限制执行环境（当前：{cur}）"
