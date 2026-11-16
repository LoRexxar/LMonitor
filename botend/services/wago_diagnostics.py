"""Wago 阶段诊断只写普通日志，不改变事件状态或新增数据库记录。"""
import json
import re
import time
import traceback
from pathlib import Path

from utils.log import logger


def safe_message(value):
    """保留可定位的错误原因，遮蔽常见认证值并限制单条日志长度。"""
    text = str(value)
    text = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[已隐藏]@', text)
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', r'\1 [已隐藏]', text)
    text = re.sub(
        r'''(?ix)(["']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization)\b["']?\s*[:=]\s*)(["'])(.*?)\2''',
        lambda m: m[1] + m[2] + '[已隐藏]' + m[2], text)
    text = re.sub(
        r'''(?ix)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\b\s*[:=]\s*)(?!["'])([^\s&,;\}\]]+)''',
        r'\1[已隐藏]', text)
    return text[:2000]


def error_details(error):
    data = {'error_type': type(error).__name__ if isinstance(error, BaseException) else 'reported_error',
            'error_message': safe_message(error)}
    if isinstance(error, BaseException):
        data['frames'] = [f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
                          for frame in traceback.extract_tb(error.__traceback__, limit=-6)]
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if cause is not None:
            data['cause_type'] = type(cause).__name__
            data['cause_message'] = safe_message(cause)
    return data


def event_context(event):
    if event is None:
        return {}
    result = {'event_type': type(event).__name__, 'event_id': event.pk}
    for field in ('branch', 'locale', 'region_id', 'from_push', 'to_push', 'from_build', 'to_build'):
        if hasattr(event, field):
            result[field] = getattr(event, field)
    return result


def run_stage(monitor, phase, operation, *, event=None, **scope):
    """嵌套阶段继承事件身份；HTTP耗时为请求累计时间，不能当作串行耗时相加。"""
    previous = getattr(monitor, '_wago_log_context', None)
    context = {**(previous or {}), **event_context(event), **scope, 'phase': phase}
    monitor._wago_log_context = context
    started = time.monotonic()
    before = monitor._http_metrics()
    logger.info('[WagoSkillDiff Stage] %s', json.dumps({**context, 'event': 'started'}, ensure_ascii=False))
    details = {'event': 'finished'}
    try:
        result = operation()
        if isinstance(result, dict) and result.get('diff_unavailable'):
            details.update(event='failed', **error_details(result.get('error') or 'Wago diff unavailable'))
        elif isinstance(result, bool):
            details['result'] = result
        return result
    except BaseException as exc:
        details.update(event='failed', **error_details(exc))
        raise
    finally:
        attempts, failures, seconds = monitor._http_metrics()
        details.update(seconds=round(time.monotonic() - started, 3),
                       http_attempts=attempts - before[0], http_failed_attempts=failures - before[1],
                       http_seconds=round(seconds - before[2], 3))
        # WARNING 留在文件/控制台，不经过 ERROR 级别的数据库告警处理器。
        log = logger.warning if details['event'] == 'failed' else logger.info
        try:
            log('[WagoSkillDiff Stage] %s', json.dumps({**context, **details}, ensure_ascii=False))
        finally:
            monitor._wago_log_context = previous
