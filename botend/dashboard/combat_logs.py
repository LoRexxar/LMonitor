"""登录用户的战斗日志，报告按上传者与分析册的共享范围访问。"""

import json
import uuid
from urllib.parse import urlencode
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from botend.dashboard.permissions import dashboard_permission_required
from django.db import transaction
from django.db.models import Count, Q
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from botend.models import CombatLogReport, CombatLogPolicy
from botend.services import combat_log_archive as archive
from botend.services import combat_loading, combat_page_cache
from botend.services.combat_wcl_account import WclError


def endpoint(*methods):
    def decorate(view):
        @wraps(view)
        def checked(request, *args, **kwargs):
            try:
                key = combat_page_cache.page_key(request)
                force_reload = request.headers.get("Sec-Fetch-Dest") == "document" and "no-cache" in request.headers.get("Cache-Control", "")
                request.combat_refresh = bool(key and (request.GET.get("refresh") or force_reload))
                entry = combat_page_cache.lookup(key) if key and not request.combat_refresh else None
                versions = None
                if entry is not None:
                    initial = request.headers.get("Sec-Fetch-Dest") == "document" and "_clwait" not in request.GET
                    versions = combat_page_cache.scope(request, quick=initial)
                    if versions == entry[0]:
                        return combat_page_cache.response(entry, request)
                with combat_loading.navigation(request) as waiting:
                    if waiting is not None:
                        return waiting
                    if key and "refresh" in request.GET:
                        query = request.GET.copy()
                        del query["refresh"]
                        request.GET = query
                        request.META["QUERY_STRING"] = query.urlencode()
                    if key and entry is None:
                        versions = combat_page_cache.scope(request)
                    response = view(request, *args, **kwargs)
                    if key:
                        combat_page_cache.remember(key, versions, request, response)
                    if request.headers.get("X-Combat-Navigation") == "continue" and response.get("Content-Type", "").startswith("text/html"):
                        response["X-Combat-Document"] = "1"
                    return response
            except CombatLogReport.DoesNotExist:
                raise Http404
            except WclError as exc:
                if getattr(request, "combat_loading_title", None):
                    response = render(request, "dashboard/combat_loading.html", {"loading_error": str(exc),
                                  "loading_title": request.combat_loading_title}, status=exc.status)
                    response["X-Combat-Document"] = "1"
                    return response
                return JsonResponse({"message": str(exc)}, status=exc.status)
            except ValidationError:
                return JsonResponse({"message": "报告标识无效。"}, status=400)
            except ValueError as exc:
                return JsonResponse({"message": str(exc)}, status=400)

        return never_cache(login_required(csrf_protect(require_http_methods(methods)(checked))))

    return decorate


@endpoint("GET")
def request_progress(request, token):
    return JsonResponse({"message": combat_loading.status(request.user, token)})


STATES = {
    "uploading": "待上传",
    "paused": "暂停上传",
    "queued": "排队中",
    "running": "解析中",
    "ready": "已就绪",
    "failed": "解析失败",
}


@endpoint("GET", "POST")
def uploader(request):
    if request.method == "POST":
        try:
            items = json.loads(request.body)
            if not isinstance(items, list) or not 1 <= len(items) <= 1000:
                raise ValueError
            batch = [(r["name"], r["size"], uuid.UUID(r["key"])) for r in items]
            if any(not isinstance(n, str) or type(s) != int for n,s,k in batch) or len({k for n,s,k in batch}) != len(batch):
                raise ValueError
        except (TypeError, ValueError, KeyError, AttributeError):
            raise ValueError("场次上传清单无效。") from None
        reports = archive.create_batch(request.user, batch)
        return JsonResponse({"reports": [reverse("combat_report", args=[r.pk]) for r in reports]})
    resume = None
    if request.GET.get("resume"):
        resume = get_object_or_404(CombatLogReport, pk=request.GET["resume"], owner=request.user,
                                   state__in=["paused","uploading"], expires_at__gt=timezone.now())
    return render(request, "dashboard/combat_upload.html", {
        "policy": archive.policy_for(request.user),
        "resume": resume if resume and resume.upload_key else None,
        "legacy_resume": resume if resume and not resume.upload_key else None,
        "max_source_bytes": getattr(settings, "COMBAT_LOG_MAX_SOURCE_BYTES", 2 * 1024**3),
        "count": CombatLogReport.objects.filter(owner=request.user, retire_after__isnull=True, expires_at__gt=timezone.now()).count(),
    })


@login_required
def legacy_combat_url(request, legacy_path=""):
    target = request.get_full_path().replace("/dashboard/combat-logs/", "/portal/combat-logs/", 1)
    return redirect(target, preserve_request=True)


def upload_policy_context(params):
    query = params.get("q", params.get("account", "")).strip()
    accounts = get_user_model().objects.only("pk", "username").order_by("username", "pk")
    if query:
        accounts = accounts.filter(username__icontains=query)
    page = Paginator(accounts, 25).get_page(params.get("page"))
    accounts = list(page)
    policies = CombatLogPolicy.objects.in_bulk(["default", *(str(account.pk) for account in accounts)])
    default = policies.get("default") or CombatLogPolicy(scope="default")
    rows = [{"scope": "default", "label": "全站默认", "policy": default}]
    for account in accounts:
        scope = str(account.pk)
        rows.append({"scope": scope, "label": account.username, "custom": scope in policies,
                     "policy": policies.get(scope, default)})
    filters = {"section": "combat-upload-policy"}
    if query:
        filters["q"] = query
    return {"policy_rows": rows, "policy_page": page, "policy_query": query,
            "policy_filters": urlencode(filters), "policy_saved": params.get("saved")}


@endpoint("GET", "POST")
@dashboard_permission_required("system.combat-upload-policy")
def upload_policy(request):
    params = {"section": "combat-upload-policy"}
    query = request.GET.get("q", request.GET.get("account", "")).strip()
    if query:
        params["q"] = query
    if request.GET.get("page"):
        params["page"] = request.GET["page"]
    if request.method == "GET":
        return redirect(reverse("dashboard") + "?" + urlencode(params))
    if "scope" in request.POST:
        scope = request.POST["scope"]
        if scope != "default":
            if not scope.isascii() or not scope.isdigit() or len(scope) > 19 or int(scope) > 2**63 - 1:
                raise ValueError("无效的账号编号。")
            scope = str(get_object_or_404(get_user_model(), pk=int(scope)).pk)
    elif "account" in request.POST:
        username = request.POST["account"].strip()
        scope = str(get_object_or_404(get_user_model(), username=username).pk) if username else "default"
    else:
        raise ValueError("请选择要修改的策略。")
    if request.POST.get("inherit"):
        if scope == "default":
            raise ValueError("请选择要恢复默认设置的账号。")
        CombatLogPolicy.objects.filter(pk=scope).delete()
    else:
        try:
            count, days = int(request.POST["max_reports"]), int(request.POST["retention_days"])
            if not 1 <= count <= 1000 or not 1 <= days <= 3650:
                raise ValueError
        except (KeyError, ValueError):
            raise ValueError("份数须在 1–1000 之间，有效期须在 1–3650 天之间。") from None
        full_action = request.POST.get("full_action", "recycle" if "recycle_oldest" in request.POST else "reject")
        if full_action not in ("reject", "recycle"):
            raise ValueError("请选择满额时的处理方式。")
        CombatLogPolicy.objects.update_or_create(scope=scope, defaults=dict(
            max_reports=count, retention_days=days, recycle_oldest=full_action == "recycle"))
    params["saved"] = scope
    return redirect(reverse("dashboard") + "?" + urlencode(params) + "#policy-row-" + scope)


@endpoint("GET")
@dashboard_permission_required("system.combat-upload-policy")
def upload_usage(request, account_id):
    account = get_object_or_404(get_user_model().objects.only("pk", "username"), pk=account_id)
    policy = archive.policy_for(account)
    reports = CombatLogReport.objects.filter(owner=account)
    now = timezone.now()
    counts = reports.aggregate(slots=Count("pk", filter=Q(retire_after__isnull=True, expires_at__gt=now)),
                               expired=Count("pk", filter=Q(expires_at__lte=now)))
    reserved, byte_limit = archive.account_byte_quota(account)
    page = Paginator(reports.order_by("-created_at", "-pk").only(
        "filename", "size", "state", "created_at", "expires_at", "retire_after_id"
    ), 10).get_page(request.GET.get("page"))
    for report in page:
        report.state_label = STATES.get(report.state, report.state)
    return render(request, "dashboard/combat_upload_usage.html", {
        "account": account, "policy": policy, "slots": counts["slots"], "expired": counts["expired"],
        "remaining": max(policy.max_reports - counts["slots"], 0), "reserved": reserved,
        "byte_limit": byte_limit, "log_page": page, "now": now,
    })


@endpoint("POST")
def remove_upload(request, pk):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        report = get_object_or_404(CombatLogReport.objects.select_for_update(),pk=pk,owner=request.user)
        if report.state in ("running","queued"):
            raise ValueError("请等待此日志解析结束。")
        report.delete()
    return redirect("combat_logs")


@endpoint("GET", "POST")
def library(request):
    if request.method == "POST":
        try:
            size = int(request.POST.get("size", ""))
        except ValueError:
            raise ValueError("无效的文件大小。") from None
        report = archive.create(request.user, request.POST.get("name", ""), size)
        return JsonResponse(
            {"url": reverse("combat_report", args=[report.pk]), "part_size": report.part_size}
        )
    from botend.dashboard.combat_library import library_page
    return library_page(request,request.GET if "name" in request.GET else None)


@endpoint("GET")
def detail(request, pk):
    from botend.dashboard.combat_workbench import visible
    from botend.dashboard.combat_wcl import source_directory
    from botend.services.combat_wcl_index import resolve

    report = visible(request.user).filter(pk=pk).first()
    if report is None:
        remote = resolve(request.user,pk)
        if not remote:
            raise Http404
        return redirect(source_directory(remote,request.GET.get("origin","")))
    context = {
        "report": report,
        "label": STATES[report.state],
        "stalled": report.updated_at < timezone.now() - archive.LEASE,
    }
    if report.published or report.state == "ready":
        fight_id = request.GET.get("fight")
        death = request.GET.get("death")
        try:
            context.update(
                archive.read_report(
                    report, int(fight_id) if fight_id else None, int(death) if death else None
                )
            )
        except (ValueError, LookupError):
            raise Http404
    return render(request, "dashboard/combat_logs.html", context)


@endpoint("GET")
def download(request, pk):
    report = get_object_or_404(CombatLogReport, pk=pk, owner=request.user, published__isnull=False, expires_at__gt=timezone.now())
    response = StreamingHttpResponse(archive.archived_chunks(report), content_type="application/octet-stream")
    response["Content-Disposition"] = content_disposition_header(True, report.filename)
    response["Content-Length"] = report.size
    return response


@endpoint("GET")
def manifest(request, pk):
    report = get_object_or_404(CombatLogReport, pk=pk, owner=request.user, expires_at__gt=timezone.now())
    return JsonResponse(
        {
            "parts": dict(report.parts.values_list("number", "digest")),
            "size": report.size,
            "part_size": report.part_size,
            "state": report.state,
        }
    )


@endpoint("PUT")
def upload_part(request, pk, number):
    data = request.read(archive.PART_SIZE + 1)
    if len(data) > archive.PART_SIZE:
        raise ValueError("分片超出大小限制。")
    archive.put_part(request.user, pk, number, data)
    return JsonResponse({"ok": True})


@endpoint("POST")
def pause(request, pk):
    archive.pause_upload(request.user, pk)
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"ok": True})
    return redirect("combat_report", pk=pk)


@endpoint("POST")
def finish(request, pk):
    archive.enqueue(request.user, pk)
    return JsonResponse({"ok": True})


@endpoint("POST")
def highlights(request, pk):
    """模型仅选择证据行，页面使用本地已核实的事件呈现结果。"""
    from core.glm import GLMClient

    report = get_object_or_404(CombatLogReport, pk=pk, owner=request.user, state="ready", expires_at__gt=timezone.now())
    try:
        result = archive.read_report(report, int(request.POST["fight"]), int(request.POST["death"]))
    except (KeyError, ValueError, LookupError):
        raise Http404
    evidence = [
        {"line": item["line"], "seconds": item["seconds"], "type": item["kind"]}
        for item in result["evidence"]
    ]
    client = GLMClient()
    if not client.available:
        return JsonResponse({"message": "当前未配置可用模型，可直接查看原始事件。"}, status=503)
    prompt = (
        "请为魔兽死亡复盘挑选最多五条最值得回看且类型互补的事件，优先呈现死亡前的受伤、治疗或光环变化。"
        "只返回由输入中行号组成的 JSON 整数数组。" + json.dumps(evidence, ensure_ascii=False)
    )
    try:
        raw = client.send_message(prompt, max_tokens=200)
        selected = json.loads(raw or "")
        allowed = {item["line"] for item in evidence}
        if (
            not isinstance(selected, list)
            or not 1 <= len(selected) <= 5
            or any(type(line) is not int or line not in allowed for line in selected)
        ):
            raise ValueError
    except Exception:
        return JsonResponse({"message": "模型未返回有效的证据行，可查看完整时间线。"}, status=502)
    return JsonResponse({"lines": sorted(set(selected))})
