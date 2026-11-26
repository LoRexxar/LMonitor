"""日志分析只读结果的短期缓存。每次复用前仍校验当前账号的报告范围与索引版本。"""
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

from django.core.cache.backends.filebased import FileBasedCache
from django.http import Http404, HttpResponse, QueryDict
from django.middleware.csrf import get_token
from django.utils.translation import get_language

from botend.services.combat_log_archive import directory


PAGES = {
    "combat_battles", "combat_analysis", "combat_workspace", "combat_player_comparison",
    "combat_actor", "combat_evidence", "combat_replay", "combat_review", "combat_wcl_report", "combat_wcl_workspace",
}
REMOTE = {"combat_wcl_report", "combat_wcl_workspace"}
MAX_BYTES = 512 * 1024


def _storage():
    root = directory(SimpleNamespace(pk="analysis-pages"), create=False)
    return FileBasedCache(str(root), {"TIMEOUT": 300, "OPTIONS": {"MAX_ENTRIES": 256}})


def page_key(request):
    name = request.resolver_match.url_name
    params = request.GET
    if (request.method != "GET" or name not in PAGES
            or not (request.headers.get("Sec-Fetch-Dest") in ("document", "empty")
                    or request.headers.get("X-Combat-Navigation") == "continue")
            or params.keys() & {"read_public", "private_report", "book", "rule", "cast_icon"}
            or params.get("channel") == "private" or params.get("panel") == "review"
            or params.get("pairing") == "public" or not request.session.session_key):
        return None
    # CSRF 掩码随渲染随机化，表单实际绑定到当前会话的密钥。
    get_token(request)
    parts = (request.session.session_key, request.user.pk, request.user.username, request.user.is_superuser,
             request.user.is_staff, request.META["CSRF_COOKIE"], request.build_absolute_uri(request.path),
             get_language(), request.headers.get("X-Combat-View", ""),
             sorted((k, v) for k, v in params.lists() if k not in ("_clwait", "refresh")))
    return "analysis-page:v3:" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def scope(request, *, quick=False):
    from botend.dashboard.combat_workbench import visible
    from botend.services import combat_wcl_index

    name = request.resolver_match.url_name
    if name in REMOTE:
        return ()
    ids = set(request.GET.getlist("log")) if name in ("combat_workspace", "combat_battles") else set()
    if pk := request.resolver_match.kwargs.get("pk"):
        ids.add(str(pk))
    if name == "combat_replay" and request.GET.get("back"):
        ids.update(QueryDict(request.GET["back"]).getlist("log"))
    ids = {str(UUID(pk)) for pk in ids}
    global_scope = not ids and (name == "combat_player_comparison" or name == "combat_workspace")
    if not ids and not global_scope:
        return ()
    reports = visible(request.user).filter(published__isnull=False)
    if not global_scope:
        reports = reports.filter(pk__in=ids)
    versions = {str(pk): (str(revision), updated) for pk, revision, updated in
                reports.order_by("pk").values_list("pk", "published", "updated_at")[:257]}
    if len(versions) > 256 or len(ids) > 256:
        return None
    # WCL 单场索引同时校验所有者、绑定状态、文件有效期和上游访问权限。
    for pk in ids - versions.keys():
        if quick and combat_wcl_index.needs_access_check(pk):
            return None
        report = combat_wcl_index.resolve(request.user, pk)
        if report is None:
            raise Http404
        versions[pk] = (str(report.published), None)
    if name == "combat_review":
        from botend.models import CombatLogReview
        params = request.resolver_match.kwargs
        saved = CombatLogReview.objects.filter(report_id=params["pk"], version=versions[str(params["pk"])][0],
                    fight=params["fight"], topic=params["topic"], anchor=params["anchor"])
        return versions, saved.values_list("token", "updated_at").first()
    return versions


def lookup(key):
    try:
        return _storage().get(key)
    except OSError:
        return None


def response(entry, request):
    _, content_type, content = entry
    result = HttpResponse(content, content_type=content_type)
    if content_type.startswith("text/html") and request.headers.get("X-Combat-Navigation") == "continue":
        result["X-Combat-Document"] = "1"
    return result


def remember(key, versions, request, result):
    content_type = result.get("Content-Type", "")
    if (versions is None or result.status_code != 200 or result.streaming or result.cookies
            or not content_type.startswith(("text/html", "application/json")) or len(result.content) > MAX_BYTES):
        return
    # 公开报告可能仍在记录，目录与 WCL 访问检查使用相同的短时间窗口。
    ttl = 30 if request.resolver_match.url_name in REMOTE or "also" in request.GET else 300
    try:
        _storage().set(key, (versions, content_type, result.content), timeout=ttl)
    except OSError:
        pass
