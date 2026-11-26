"""选择日志和选择战斗的公共入口。来源只决定目录数据的读取方式。"""

import re
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from django.utils import timezone as django_timezone
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from botend.dashboard import combat_wcl, combat_workbench
from botend.dashboard.combat_logs import STATES, endpoint
from botend.models import CombatPublicReportEntry, CombatPublicReportPage, CombatPublicSource, WclUserBinding
from botend.services import combat_log_analysis as analysis
from botend.services import combat_loading
from botend.services import combat_public_list_cache as list_cache
from botend.services import combat_wcl_account as wcl, combat_wcl_catalog as catalog


SOURCE_KEYS = ("log", "also", "private_report")
LIST_KEYS = ("source","q","kind","realm","region","name","channel","page","wpage","saved_page")


def fields(params):
    return [(k,v) for k in SOURCE_KEYS for v in dict.fromkeys(params.getlist(k))]


def directory_url(items):
    return reverse("combat_battles") + "?" + urlencode(items)


def back_directory(raw):
    if not raw:
        return ""
    params = QueryDict(raw)
    return directory_url([(k,params[k]) for k in (*LIST_KEYS,"focus") if k in params] + fields(params))


@endpoint("POST")
def refresh_public_page(request, pk, page):
    source = get_object_or_404(CombatPublicSource, user=request.user, pk=pk)
    if not 1 <= page <= 100000:
        raise Http404
    states = CombatPublicReportPage.objects.filter(source=source, page=page)
    started = None if request.POST.get("poll") else list_cache.claim(source, page, request.POST.get("force") == "1")
    if started:
        try:
            identity = {k:getattr(source,k) for k in ("name","kind","realm","region")}
            listing = combat_wcl.public_listing(request, {}, page, identity)
            list_cache.save(source, page, listing, started=started, leased=True)
        finally:
            states.filter(refresh_started=started).update(refresh_started=None)
    state = get_object_or_404(states)
    if state.refresh_started and state.refresh_started > django_timezone.now() - list_cache.LEASE:
        return JsonResponse({"pending":state.refresh_started.isoformat()}, status=202)
    if not state.updated_at or request.POST.get("poll", "") > state.updated_at.isoformat():
        return JsonResponse({"message":"目录尚未更新，请重新尝试。"}, status=503)
    return JsonResponse({"revision":state.revision, "updated":state.updated_at.isoformat(),
                         "label":django_timezone.localtime(state.updated_at).strftime("%m-%d %H:%M")})


def library_page(request, lookup=None, *, private_binding=None):
    q = request.GET
    selected = fields(q)
    sources = CombatPublicSource.objects.filter(user=request.user).order_by("-pk")
    source_id = q.get("source", "")
    private = source_id == "private" or not source_id and q.get("channel") == "private"
    source = None
    if source_id:
        lookup = None
    if lookup is None and source_id != "uploads" and not private:
        if source_id and not source_id.isdecimal():
            raise ValueError("日志来源无效。")
        source = get_object_or_404(sources,pk=source_id) if source_id else sources.first()
        if source:
            lookup = {k:getattr(source,k) for k in ("name","kind","realm","region")}
    carry = [(k,q[k]) for k in LIST_KEYS if q.get(k)]
    if source and not source_id:
        carry.append(("source",str(source.pk)))
    state = carry + selected
    if q.get("analyze") and selected:
        return redirect(directory_url(state))
    binding = (private_binding or combat_wcl.binding_for(request.user)) if private else None
    private_available = bool(binding) if private else WclUserBinding.objects.filter(
        user=request.user, wcl_id__isnull=False).exists()
    ctx = dict(sources=sources,source=source,source_id=source_id,private=private,
               query=q.get("q","").strip(),need_selection=bool(q.get("analyze")),
               books=combat_workbench.collections(request.user).select_related("owner").only(
                   "title", "owner__username", "created_at").order_by("-created_at"),
               private_available=private_available)
    reports = combat_workbench.visible(request.user).select_related("owner").only(
        "filename", "created_at", "published", "summary", "state", "owner__username").order_by("-created_at","pk")
    if ctx["query"]:
        reports = reports.filter(Q(filename__icontains=ctx["query"]) | Q(summary__activity_index__icontains=ctx["query"]))
    page = Paginator(reports,30).get_page(1 if q.get("filter") else q.get("page"))
    rows = []
    for r in page:
        ready = bool(r.published and r.summary.get("index_version",0) >= analysis.VERSION)
        key = ("log",str(r.pk))
        archive = reverse("combat_report",args=[r.pk])
        detail = STATES.get(r.state, r.state)
        fights = r.summary.get("activity_index")
        if ready and fights:
            names = list(dict.fromkeys(f["name"] for f in fights if f.get("name")))
            detail = " · ".join(filter(None, (f"{len(fights)} 场", "、".join(names[:3]))))
        rows.append(dict(field=key[0],value=key[1],title=r.filename,date=r.created_at,
                         source="上传日志",owner=r.owner.username,detail=detail,
                         selectable=ready,archive=archive,url=directory_url(carry+[key]) if ready else archive))
    public_rows = []
    remote_page = 1
    try:
        remote_page = 1 if q.get("filter") else int(q.get("wpage", q.get("page","1") if request.resolver_match.url_name=="combat_wcl" else "1"))
        if not 1 <= remote_page <= 100000:
            raise ValueError("报告页码无效。")
        listing = None
        if private and binding:
            listing = wcl.read(binding,wcl.REPORTS,dict(user=binding.wcl_id,page=remote_page))["reportData"]["reports"]
        elif private:
            ctx["error"] = "请先绑定有权限的 WCL 账号。"
        elif lookup:
            saved = list_cache.read(source, remote_page) if source else None
            refresh = q.get("refresh")
            refresh_in_page = saved and refresh == "1" and request.headers.get("Sec-Fetch-Dest") == "document"
            ctx["force_sync"] = bool(refresh_in_page)
            if not saved or (refresh and not refresh_in_page):
                with combat_loading.navigation(request, public_list=True, source=source) as waiting:
                    if waiting is not None:
                        return waiting
                    combat_loading.message("正在向 WCL 获取公开日志列表。")
                    started = django_timezone.now()
                    try:
                        listing = combat_wcl.public_listing(request,ctx,remote_page,lookup)
                    except wcl.WclError as exc:
                        if not saved or exc.status != 503:
                            raise
                        ctx["list_warning"] = "WCL 暂时无法更新，当前显示上次成功读取的列表。"
                    else:
                        if source and listing is not None:
                            list_cache.save(source, remote_page, listing, started=started)
                            saved = list_cache.read(source, remote_page)
        entries = []
        if source and saved:
            records = CombatPublicReportEntry.objects.filter(Q(page=remote_page) | Q(code__in=saved.codes),
                                                             source=source).order_by("-started_at","code")
            reset_page = q.get("filter") or len(q.getlist("wpage")) > 1
            saved_page = Paginator(records, list_cache.PAGE_SIZE).get_page(1 if reset_page else q.get("saved_page",1))
            entries = [(e.code,e.started_at,e.labels) for e in saved_page]
            ctx.update(list_updated=saved.updated_at, list_revision=saved.revision, saved_page=saved_page,
                       sync_url=reverse("combat_refresh_public_page",args=[source.pk,remote_page]), listed=True,
                       remote_more=saved.has_more or CombatPublicReportPage.objects.filter(
                           source=source, page__gt=remote_page, updated_at__isnull=False).exists())
        elif listing is not None:
            entries = list_cache.summaries(listing)
            ctx.update(remote_more=listing["has_more_pages"],listed=True)
        for code, date, labels in entries:
            key = ("private_report" if private else "also",code)
            url = directory_url(carry+[key])
            public_rows.append(dict(code=code,url=url))
            if ctx["query"].casefold() not in (labels["title"]+labels["detail"]+labels["original"]).casefold():
                continue
            rows.append(dict(field=key[0],value=code,title=labels["title"],date=date,owner=labels["owner"],
                             source="WCL 私有渠道" if private else "WCL 公开",detail=labels["detail"],url=url,selectable=True))
    except (ValueError,wcl.WclError) as exc:
        ctx["error"] = str(exc)
    visible_keys = set()
    for row in rows:
        key = (row["field"],row["value"])
        row["checked"] = key in selected
        if row["selectable"]:
            visible_keys.add(key)
    ctx.update(rows=sorted(rows,key=lambda r:r["date"],reverse=True),page=page,
               reports=public_rows,off_page=[s for s in selected if s not in visible_keys],
               selected_count=len(selected), remote_page=remote_page,
               clear_url=reverse("combat_logs")+("?" + urlencode(carry) if carry else ""),
               lookup_fields=[(k,v) for k,v in carry if k in ("kind","name","realm","region") and lookup and not source])
    return render(request,"dashboard/combat_library.html",ctx)


def local_groups(reports):
    samples, _, skipped = analysis.catalog(reports)
    groups = {}
    for s in samples:
        group = groups.setdefault(s["cohort"],dict(key="local-"+sha256(s["cohort"].encode()).hexdigest()[:24],name=s["name"],dungeon=s["kind"]=="钥匙",
                                  dungeon_boss=bool(s["parent"]),
                                  difficulty=(f"大秘境 +{s['level']}" if s["kind"]=="钥匙" else
                                              f"{'副本首领' if s['parent'] else '团本首领'} · 难度 {s['level']}"),
                                  pulls=[],kills=0,wipes=0,source_label="上传日志"))
        start,end = s["start_ms"],s["end_ms"]
        date = datetime(1,1,1,tzinfo=timezone.utc)+timedelta(milliseconds=start-86400000) if start and start>=86400000 else None
        seconds = int((end-start)/1000) if start is not None and end is not None else None
        kill = s["result"] in ("已完成","击杀")
        url = s["url"]
        if len(reports)>1:
            url = reverse("combat_workspace")+"?"+urlencode([*(("log",str(r.pk)) for r in reports),
                                                             ("sample",str(s["report"].pk)),("fight",s["id"])])
        group["pulls"].append(dict(number=0,date=date,result=s["result"],file=s["report"].filename,
                                   elapsed=f"{seconds//60}:{seconds%60:02d}" if seconds is not None else "—",
                                   url=url,kill=kill,local=True))
        group["kills"] += int(kill)
        group["wipes"] += int(s["result"] in ("未完成","未击杀","灭团"))
    for cohort,g in groups.items():
        items = [("log",str(r.pk)) for r in reports]+[("cohort",cohort)]
        base = reverse("combat_workspace")+"?"
        g["combined_url"] = base+urlencode(items)
        g["players_url"] = base+urlencode(items+[("mode","players"),("target_group",cohort)])
        if not g["dungeon"] and (not g["dungeon_boss"] or len(g["pulls"]) > 1):
            g["review_url"] = base+urlencode(items+[("panel","boss")])
        g["pulls"].sort(key=lambda p:(p["date"] is None,p["date"]))
        for n,p in enumerate(g["pulls"],1):
            p["number"]=n
    return list(groups.values()), skipped


@endpoint("GET")
def battles(request):
    q = request.GET
    items = fields(q)
    if not items:
        return redirect(reverse("combat_logs")+"?analyze=battle")
    local = combat_workbench.selected_reports(request.user,q)
    groups, skipped = local_groups(local)
    report_count = len(local)
    picked_ref = ""
    picked_group = ""
    state = [(k,q[k]) for k in LIST_KEYS if q.get(k)]
    library_url = reverse("combat_logs")+"?"+urlencode(state+items)
    try:
        for private,field in ((False,"also"),(True,"private_report")):
            codes = list(dict.fromkeys(q.getlist(field)))
            if not codes:
                continue
            if len(codes)>20 or any(not re.fullmatch(r"[A-Za-z0-9]{16}",c) for c in codes):
                raise ValueError("每个 WCL 渠道最多可选择 20 份有效报告。")
            binding = combat_wcl.binding_for(request.user) if private else None
            if private and not binding:
                raise wcl.AccessDenied("私有报告需要先授权 WCL 账号。")
            combat_loading.message(f"正在读取 {len(codes)} 份 WCL 报告的战斗目录。")
            remote = catalog.fetch_reports(codes,binding)
            report_count += len(remote)
            titles = {r["code"]:r["title_label"] for r in remote}
            pick = q.get("pick") if len(items) == 1 else None
            if pick == "last" and remote and remote[0]["fights"]:
                pick = remote[0]["fights"][-1]["id"]
            for g in catalog.attempts(remote):
                matching = [r["code"] for r in remote if g["key"] in r["keys"]]
                if not g["dungeon"]:
                    g["review_url"] = combat_wcl.workspace_url(matching,private,group=g["key"],selection=1)
                g.update(key=field+"-"+g["key"],source_label="WCL 私有渠道" if private else "WCL 公开")
                for p in g["pulls"]:
                    for view in p["views"]:
                        view["single_url"] = combat_wcl.single_url(view["code"], view["id"], private, "")
                        view["source_title"] = titles[view["code"]]
                    if pick and any(v["ref"] == f"{codes[0]}.{pick}" for v in p["views"]):
                        picked_ref, picked_group = p["ref"], g["key"]
                    minutes,seconds=divmod(int(p["duration"]),60)
                    p.update(elapsed=f"{minutes}:{seconds:02d}",file=titles[p["code"]],
                             url=combat_wcl.single_url(p["code"],p["id"],private,""))
                groups.append(g)
    except (ValueError,wcl.WclError) as exc:
        status = exc.status if isinstance(exc, wcl.WclError) else 400
        return render(request,"dashboard/combat_battles.html",dict(error=str(exc),listing_url=library_url),status=status)
    for g in groups:
        location = state+items+[("focus",g["key"])]
        g["focus_url"] = directory_url(location)
        back = urlencode({"back_catalog":urlencode(location)})
        for key in ("review_url","players_url","combined_url"):
            if g.get(key):
                g[key] += "&"+back
        for pull in g["pulls"]:
            pull["url"] += ("&" if "?" in pull["url"] else "?")+back
            for view in pull.get("views", ()):
                view["single_url"] += ("&" if "?" in view["single_url"] else "?")+back
    active = next((g for g in groups if g["key"]==(q.get("focus") or picked_group)),groups[0] if groups else None)
    return render(request,"dashboard/combat_battles.html",dict(groups=groups,active=active,report_count=report_count,
                  shared_directory=True,listing_url=library_url,picked=picked_ref,skipped=skipped,pull_count=sum(len(g["pulls"]) for g in groups)))
