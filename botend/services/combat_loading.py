"""原生页面导航等待期间的阶段提示，仅短暂保存展示文本。"""
import re
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from uuid import uuid4

from django.core.cache.backends.filebased import FileBasedCache
from django.shortcuts import render
from django.urls import reverse

_active_key = ContextVar("combat_loading_key", default=None)
PARAM = "_clwait"
PAGES = {
    "combat_logs": "读取日志列表",
    "combat_battles": "整理所选日志的战斗目录",
    "combat_wcl_report": "读取 WCL 战斗报告",
    "combat_wcl_workspace": "读取所选 WCL 报告",
    "combat_wcl_single": "准备单场战斗事件索引",
    "combat_analysis": "读取单场战斗数据",
    "combat_workspace": "汇总所选战斗数据",
    "combat_player_comparison": "读取角色对比数据",
    "combat_collection": "读取团队分析册",
    "combat_actor": "读取角色的战斗明细",
    "combat_evidence": "读取战斗事件原文",
    "combat_review": "读取战斗复盘记录",
    "combat_replay": "准备战斗回放",
}


def _cache():
    from botend.services.combat_log_archive import directory
    folder = directory(SimpleNamespace(pk="request-progress"), create=False)
    return FileBasedCache(str(folder), {"TIMEOUT": 300, "OPTIONS": {"MAX_ENTRIES": 256}})


def message(value):
    key = _active_key.get()
    if key:
        try:
            _cache().set(key, value)
        except OSError:
            pass


def status(user, token):
    try:
        return _cache().get(f"combat:{user.pk}:{token.hex}")
    except OSError:
        return None


def page_title(request, public_list=False, index_build=False):
    continuation = (request.headers.get("X-Combat-Navigation") == "continue" and
                    re.fullmatch(r"[a-f0-9]{32}", request.GET.get(PARAM, "")))
    if (request.method != "GET" or (request.headers.get("Sec-Fetch-Dest") != "document" and not continuation)
            or "text/html" not in request.headers.get("Accept", "")):
        return None
    name = request.resolver_match.url_name
    if public_list:
        return "读取公开日志列表"
    if name == "combat_wcl_single" and not index_build:
        return None
    if name == "combat_logs" and (request.GET.get("source") != "private" and request.GET.get("channel") != "private" or request.GET.get("analyze")):
        return None
    if name == "combat_wcl" and request.GET.get("channel") == "private" and "settings" not in request.GET:
        return "读取 WCL 日志列表"
    if name in ("combat_wcl_report", "combat_wcl_workspace"):
        if request.GET.get("compare"):
            return "对比首领战斗场次"
        if request.GET.get("group"):
            return "读取首领开荒复盘"
    if name == "combat_workspace" and request.GET.get("mode") == "players":
        return "读取角色对比数据"
    if any(k in request.GET for k in ("chunk", "event_page", "cast_icon", "threshold", "options")):
        return None
    return PAGES.get(name)


@contextmanager
def navigation(request, public_list=False, source=None, index_build=False):
    if _active_key.get():
        yield None
        return
    title = page_title(request, public_list, index_build)
    token = request.GET.get(PARAM, "")
    if title and not re.fullmatch(r"[a-f0-9]{32}", token):
        ident = uuid4()
        query = request.GET.copy()
        if getattr(request, "combat_refresh", False):
            query["refresh"] = "1"
        query[PARAM] = ident.hex
        from botend.dashboard.combat_library import LIST_KEYS, SOURCE_KEYS, back_directory
        library_url = reverse("combat_logs")
        if public_list or request.resolver_match.url_name == "combat_logs":
            # 上游不可用时仍提供能立即打开的日志库入口。
            library_url += "?source=uploads"
        elif request.resolver_match.url_name == "combat_battles":
            retained = request.GET.copy()
            for key in list(retained):
                if key not in (*LIST_KEYS, *SOURCE_KEYS):
                    del retained[key]
            if retained:
                library_url += "?" + retained.urlencode()
        kwargs = request.resolver_match.kwargs
        scope = ""
        if source:
            scope = f"{source.name} · {source.realm} · {source.region.upper()}"
        elif kwargs.get("code"):
            scope = f"WCL 报告 {kwargs['code']}"
            if kwargs.get("fight"):
                scope += f" · 第 {kwargs['fight']} 场"
        else:
            count = sum(len(set(request.GET.getlist(k))) for k in ("log", "also", "private_report"))
            if count:
                scope = f"已选 {count} 份日志"
        yield render(request, "dashboard/combat_loading.html", {
            "loading_title": title, "continue_url": request.path + "?" + query.urlencode(),
            "loading_scope": scope,
            "library_url": library_url,
            "progress_url": reverse("combat_request_progress", args=[ident]),
            "directory_url": back_directory(request.GET.get("back_catalog")),
        })
        return
    if not title:
        yield None
        return
    query = request.GET.copy()
    del query[PARAM]
    request.GET = query
    request.META["QUERY_STRING"] = query.urlencode()
    request.combat_loading_title = title
    context = _active_key.set(f"combat:{request.user.pk}:{token}")
    try:
        message("服务器已收到请求，正在读取分析数据。")
        yield None
    finally:
        _active_key.reset(context)
