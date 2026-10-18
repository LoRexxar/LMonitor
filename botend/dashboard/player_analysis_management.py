"""Independent, superuser-only management of existing player analyses.

These views only read or edit persisted facts through the management service;
recheck only enqueues a detached worker; GET never collects or invokes AI.
"""
import json

from django.core.exceptions import ObjectDoesNotExist, RequestDataTooBig, ValidationError
from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie


MAX_JSON_BODY_BYTES = 128 * 1024


def _error(message, status):
    return JsonResponse({'status': 'error', 'message': message}, status=status)


def _success(data):
    return JsonResponse({'status': 'success', 'data': data})


class _InvalidJSON(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJSON('Duplicate object key')
        result[key] = value
    return result


def _reject_constant(value):
    raise _InvalidJSON('Non-finite JSON number')


def _json_payload(request):
    # Check declared length before reading; check actual length as well rather
    # than trusting a client-provided header as the only limit.
    try:
        length = int(request.META.get('CONTENT_LENGTH') or 0)
    except (ValueError, TypeError):
        raise _InvalidJSON('Invalid content length') from None
    if length > MAX_JSON_BODY_BYTES:
        raise RequestDataTooBig()
    if request.content_type != 'application/json':
        raise _InvalidJSON('JSON content type required')
    raw = request.body
    if len(raw) > MAX_JSON_BODY_BYTES:
        raise RequestDataTooBig()
    try:
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _InvalidJSON('Invalid JSON') from None
    if not isinstance(payload, dict):
        raise _InvalidJSON('JSON object required')
    return payload


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
class _SuperuserView(View):
    """Match authoritative database/AI management, not menu permissions."""
    http_method_names = ['get']

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return _error('仅超级管理员可管理角色分析', 403)
        return self._dispatch_authorized(request, *args, **kwargs)

    def _dispatch_authorized(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def http_method_not_allowed(self, request, *args, **kwargs):
        response = _error('此端点不支持该请求方法', 405)
        response['Allow'] = ', '.join(method.upper() for method in self.http_method_names)
        return response


@method_decorator(ensure_csrf_cookie, name='dispatch')
class PlayerAnalysisManagementPage(_SuperuserView):
    def get(self, request):
        return redirect('/dashboard/?section=player-analyses')


class _AnalysisAPIView(_SuperuserView):
    def _dispatch_authorized(self, request, *args, **kwargs):
        # Import only after authorization. The legacy page only redirects
        # and does not initialize any analysis/collector state.
        from botend.services import player_analysis_management as service

        try:
            return super()._dispatch_authorized(request, *args, **kwargs)
        except RequestDataTooBig:
            return _error('JSON 请求体不能超过 128 KiB', 413)
        except _InvalidJSON:
            return _error('请求必须是有效的 JSON 对象', 400)
        except ValidationError as error:
            return _error('；'.join(error.messages), 400)
        except service.AnalysisConflict:
            return _error('分析记录已变更或已存在，请刷新后重试', 409)
        except ObjectDoesNotExist:
            return _error('角色或分析记录不存在', 404)
        except DatabaseError:
            # Never expose database errors, SQL, credentials, or request bodies.
            return _error('角色分析暂不可用，请稍后重试', 503)
        except Exception:
            return _error('角色分析请求处理失败，请稍后重试', 500)


class PlayerAnalysisCollectionAPI(_AnalysisAPIView):
    http_method_names = ['get', 'post']

    def get(self, request):
        from botend.services.player_analysis_management import list_analyses
        return _success(list_analyses(request.GET))

    def post(self, request):
        from botend.services.player_analysis_management import create_analysis
        return _success(create_analysis(_json_payload(request), request.user))


class PlayerAnalysisDetailAPI(_AnalysisAPIView):
    http_method_names = ['get', 'patch', 'delete']

    def get(self, request, analysis_id):
        from botend.services.player_analysis_management import get_analysis
        return _success(get_analysis(analysis_id))

    def patch(self, request, analysis_id):
        from botend.services.player_analysis_management import update_analysis
        return _success(update_analysis(analysis_id, _json_payload(request), request.user))

    def delete(self, request, analysis_id):
        from botend.services.player_analysis_management import archive_analysis
        return _success(archive_analysis(analysis_id, _json_payload(request), request.user))


class PlayerAnalysisRecheckAPI(_AnalysisAPIView):
    http_method_names = ['get', 'post']

    def get(self, request, analysis_id):
        from botend.services.player_analysis_recheck import get_state
        return _success(get_state(analysis_id))

    def post(self, request, analysis_id):
        from botend.services.player_analysis_recheck import enqueue
        return _success(enqueue(analysis_id, _json_payload(request)))


class PlayerAnalysisCharactersAPI(_AnalysisAPIView):
    def get(self, request):
        from botend.services.player_analysis_management import list_characters
        return _success(list_characters(request.GET))
