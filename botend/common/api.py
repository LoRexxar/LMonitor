"""Shared API base view and boundary parameter parsers.

Enforces the architectural rule: validate and coerce untrusted inputs once at
the HTTP boundary, and handle errors centrally instead of repeating broad
try/except boilerplate inside every view method.
"""
import json
from typing import Any

from django.http import JsonResponse
from django.views import View

from utils.log import logger

_TRUE_STRINGS = frozenset({'1', 'true', 'yes', 'on'})
_FALSE_STRINGS = frozenset({'0', 'false', 'no', 'off'})


class ApiError(Exception):
    """Structured domain/validation error converted into a JSON response by BaseJsonAPIView."""

    def __init__(self, message: str, *, status: int = 400, code: str | None = None, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = dict(extra or {})

    def to_response(self) -> JsonResponse:
        payload: dict[str, Any] = {'success': False, 'error': self.message}
        if self.code:
            payload['code'] = self.code
        payload.update(self.extra)
        return JsonResponse(payload, status=self.status)


def parse_text(value: Any, default: str = '') -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def parse_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def parse_bounded_int(
    value: Any,
    *,
    default: int,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    parsed = parse_int(value, default=default)
    assert parsed is not None
    if min_value is not None and parsed < min_value:
        parsed = min_value
    if max_value is not None and parsed > max_value:
        parsed = max_value
    return parsed


def require_int(value: Any, *, error: str = '参数必须为整数', status: int = 400) -> int:
    parsed = parse_int(value, default=None)
    if parsed is None:
        raise ApiError(error, status=status)
    return parsed


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    return default


def parse_json_body(request, *, allow_empty: bool = True) -> dict[str, Any]:
    raw = request.body
    if not raw or not raw.strip():
        if allow_empty:
            return {}
        raise ApiError('请求体不能为空', status=400)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError('请求体不是有效的 JSON', status=400) from exc
    if not isinstance(payload, dict):
        raise ApiError('请求体必须是 JSON 对象', status=400)
    return payload


class BaseJsonAPIView(View):
    """Base view providing centralized ApiError & unexpected exception handling."""

    error_prefix: str = ''
    unexpected_error_status: int = 500

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except ApiError as exc:
            return exc.to_response()
        except Exception as exc:
            prefix = self.error_prefix or self.__class__.__name__
            logger.exception('%s 未处理异常: %s', prefix, exc)
            message = f'{self.error_prefix}: {exc}' if self.error_prefix else str(exc)
            return JsonResponse(
                {'success': False, 'error': message},
                status=self.unexpected_error_status,
            )

    @staticmethod
    def ok(data: Any = None, *, status: int = 200, **extra) -> JsonResponse:
        payload: dict[str, Any] = {'success': True}
        if data is not None:
            payload['data'] = data
        payload.update(extra)
        return JsonResponse(payload, status=status)

    @staticmethod
    def fail(error: str, *, status: int = 400, **extra) -> JsonResponse:
        payload: dict[str, Any] = {'success': False, 'error': error}
        payload.update(extra)
        return JsonResponse(payload, status=status)
