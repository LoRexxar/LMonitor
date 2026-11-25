"""日志库中的 WCL 账号管理和按需读取报告。"""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core.cache import cache
from django.http import Http404, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from botend.dashboard.combat_logs import endpoint
from botend.models import CombatPublicSource, UserCharacterBinding, WclUserBinding, WowRealmDirectory
from django.db.models import Q
from botend.services import combat_log_localization as localization
from botend.services import combat_wcl_account as wcl
from botend.services import combat_wcl_catalog as catalog
from botend.services import combat_wcl_index
from botend.services import combat_loading
from botend.services import combat_public_roles as roles
from botend.dashboard.combat_raid_study import progress_chart


def search_context(params):
    return urlencode({k:params[k][:120] for k in ("kind","name","realm","region","page","channel") if params.get(k)})


def workspace_url(codes, private, origin="", **options):
    params = dict(options)
    if private:
        params["channel"] = "private"
    if origin:
        params["origin"] = origin
    if len(codes)>1:
        params["also"] = codes[1:]
    query = urlencode({k:v for k,v in params.items() if v is not None},doseq=True)
    return reverse("combat_wcl_report", args=[codes[0]]) + ("?" + query if query else "")


def binding_for(user):
    return WclUserBinding.objects.filter(user=user, wcl_id__isnull=False).first()


def source_directory(report, origin=""):
    source = report.wcl_source
    return workspace_url([source["code"]], source["account"] is not None,
                         search_context(QueryDict(origin)), focus=source["focus"])


def listing_url(origin,private):
    params = QueryDict(origin,mutable=True)
    if private:
        params["source"] = "private"
    if "page" in params:
        params["wpage"] = params.pop("page")[-1]
    return reverse("combat_logs") + ("?"+params.urlencode() if params else "")


def single_url(code, fight, private, origin, back_catalog=None):
    path = reverse("combat_wcl_single",args=[code,fight])
    args = {}
    if private:
        args["channel"] = "private"
    if origin:
        args["origin"] = origin
    if back_catalog:
        args["back_catalog"] = back_catalog
    return path + ("?" + urlencode(args) if args else "")


@endpoint("GET")
def single(request, code, fight):
    if not re.fullmatch(r"[a-zA-Z0-9]{16}",code) or fight < 1:
        raise Http404
    private = request.GET.get("channel") == "private"
    binding = binding_for(request.user) if private else None
    origin = search_context(QueryDict(request.GET.get("origin","")))
    try:
        if private and not binding:
            raise wcl.AccessDenied("请先绑定有权读取这场战斗的 WCL 账号。")
        key = combat_wcl_index.materialize(request.user,binding,code,fight)
    except wcl.WclError as exc:
        directory = workspace_url([code],private,origin)
        return render(request,"dashboard/combat_wcl.html",
                      dict(section="report",error=str(exc),private=private,directory_url=directory,
                           listing_url=listing_url(origin,private)),
                      status=exc.status)
    args = {"fight":fight}
    if request.GET.get("back_catalog"):
        args["back_catalog"] = request.GET["back_catalog"]
    if origin:
        args["origin"] = origin
    return redirect(reverse("combat_analysis",args=[key]) + "?" + urlencode(args))


def back(request, message):
    request.session["combat_wcl_notice"] = message
    response = redirect(reverse("combat_wcl") + "?channel=private&settings=1")
    response["Referrer-Policy"] = "no-referrer"
    return response


@endpoint("POST")
def connect(request):
    try:
        return redirect(wcl.authorize(request))
    except wcl.WclError as exc:
        return back(request, str(exc))


@never_cache
@require_GET
def callback(request):
    if not request.user.is_authenticated:
        return back(request, "请登录后重新发起 WCL 绑定。")
    try:
        wcl.complete(request)
    except wcl.WclError as exc:
        return back(request, str(exc))
    return back(request, "WCL 账号已绑定。")


@endpoint("POST")
def disconnect(request):
    request.session.pop(wcl.SESSION_KEY, None)
    WclUserBinding.objects.filter(user=request.user).delete()
    return back(request, "已解除本站绑定并清除保存的 WCL 用户凭据。")


def report_reference(value):
    value = value.strip()
    if re.fullmatch(r"[a-zA-Z0-9]{16}", value):
        return value, ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in ("www.warcraftlogs.com", "cn.warcraftlogs.com", "warcraftlogs.com"):
        raise ValueError("请输入 WCL 正式服报告链接或 16 位报告代码。")
    match = re.fullmatch(r"/reports/([a-zA-Z0-9]{16})/?", parsed.path)
    if not match:
        raise ValueError("未找到 WCL 报告代码。")
    query = {**parse_qs(parsed.fragment), **parse_qs(parsed.query)}
    fight = query.get("fight", [""])[0]
    return match[1], fight if fight.isdecimal() or fight == "last" else ""


def first_page_key(user, source):
    return f"combat-public-first-page:{user.pk}:{source.pk}"


def public_listing(request, ctx, page, q):
    kind = q.get("kind", "character")
    name, realm, region = [q.get(k, "").strip() for k in ("name", "realm", "region")]
    region = region.lower()
    if kind not in ("character", "guild") or region not in ("", "cn", "tw", "kr", "us", "eu"):
        raise ValueError("查询类型或地区无效。")
    if not name or len(name) > 100 or len(realm) > 100:
        raise ValueError("请填写有效的角色或公会名称。")
    if not realm or not region:
        known = UserCharacterBinding.objects.filter(user=request.user, is_active=True, character_name__iexact=name)
        if region:
            known = known.filter(region__iexact=region)
        if realm:
            known = known.filter(realm_slug__iexact=realm)
        matches = list(known.order_by().values_list("realm_slug","region").distinct()) if kind == "character" else []
        if len(matches) == 1:
            realm, region = matches[0]
        elif matches:
            ctx["matches"] = [(r, s, "?" + urlencode(dict(kind=kind,name=name,realm=r,region=s))) for r,s in matches]
            return None
        else:
            raise ValueError("请补充地区和服务器，以定位这个名字对应的角色或公会。")
    directory = WowRealmDirectory.objects.filter(region__iexact=region).filter(
        Q(name__iexact=realm) | Q(localized_name__iexact=realm) | Q(slug__iexact=realm))
    realms = list(directory.values_list("slug",flat=True).distinct()[:2])
    if len(realms)>1:
        raise ValueError("该服务器名称匹配多个记录，请使用 WCL 服务器标识。")
    variables = dict(name=name,realm=realms[0] if realms else realm.lower(),region=region.upper(),page=page)
    ctx["subject"] = f"{name} · {variables['region']} / {variables['realm']}"
    ctx["identity"] = dict(kind=kind,name=name,realm=variables["realm"],region=region)
    if kind == "guild":
        return wcl.read(None,wcl.GUILD_REPORTS,variables)["reportData"]["reports"]
    char = wcl.read(None,wcl.CHARACTER_REPORTS,variables)["characterData"]["character"]
    if not char:
        raise ValueError("WCL 没有找到这个角色，请核对名字、地区和服务器。")
    return char["recentReports"]


@endpoint("GET","POST")
def home(request):
    private = request.GET.get("channel") == "private"
    binding = binding_for(request.user) if private else None
    ctx = dict(binding=binding, configured=wcl.configured(), callback=wcl.callback_uri(request),
               notice=request.session.pop("combat_wcl_notice", ""), section="account", private=private)
    status = 200
    try:
        if "link" in request.GET:
            from botend.dashboard.combat_library import directory_url
            code, fight = report_reference(request.GET["link"])
            items = [("private_report" if private else "also", code)]
            if private:
                items.append(("source", "private"))
            if fight:
                items.append(("pick", fight))
            return redirect(directory_url(items))
        if request.method == "POST":
            if request.POST.get("bind_role"):
                bound = get_object_or_404(roles.bound_roles(request.user), pk=int(request.POST["bind_role"]))
                roles.add(request.user, dict(kind="character", name=bound.character_name, realm=bound.realm_slug,
                          region=bound.region.lower()), roles.bound_scope(bound), bound.realm_slug)
                return redirect("combat_wcl")
            if request.POST.get("refresh_spec"):
                source = get_object_or_404(CombatPublicSource, user=request.user, kind="character",
                                           pk=int(request.POST["refresh_spec"]))
                source = roles.refresh_profile(source)
                if request.headers.get("Accept") == "application/json":
                    return JsonResponse({"label": source.spec_label, "icon": source.spec_icon})
                return redirect("combat_wcl")
            if request.POST.get("detach"):
                CombatPublicSource.objects.filter(user=request.user,pk=int(request.POST["detach"])).delete()
                return redirect("combat_wcl")
            if request.POST.get("kind", "character") == "character":
                existing = CombatPublicSource.objects.filter(user=request.user, kind="character")
                if (existing.count() >= roles.limit_for(request.user)
                        and not existing.filter(name__iexact=request.POST.get("name", "").strip()).exists()):
                    raise ValueError("公开角色名额已满，请先取消一个关联。")
            listing = public_listing(request,ctx,1,request.POST)
            if listing is not None:
                identity = ctx["identity"]
                scope, realm = (roles.profile(identity["name"], identity["region"], identity["realm"])
                                if identity["kind"] == "character" else (None, ""))
                source = roles.add(request.user, identity, scope, realm)
                cache.set(first_page_key(request.user, source), listing, timeout=45)
                return redirect(reverse("combat_logs") + "?" + urlencode({"source":source.pk}))
        elif "name" in request.GET or private and binding and not request.GET.get("settings"):
            from botend.dashboard.combat_library import library_page
            return library_page(request, request.GET if not private else None, private_binding=binding)
    except (wcl.WclError, ValueError) as exc:
        status = exc.status if isinstance(exc, wcl.WclError) else 400
        if request.POST.get("refresh_spec") and request.headers.get("Accept") == "application/json":
            return JsonResponse({"error": str(exc)}, status=status)
        ctx["error"] = str(exc)
    ctx["lookup"] = request.POST if request.method=="POST" else request.GET
    if not private:
        ctx.update(roles.roster(request.user))
    return render(request, "dashboard/combat_wcl.html", ctx, status=status)


def details(report, fight):
    master = report.get("masterData") or {}
    if (not isinstance(master.get("actors"), list) or not isinstance(master.get("abilities"), list)
            or not isinstance((report.get("deaths") or {}).get("data"), list)):
        raise wcl.WclError("该报告的事件或角色明细尚不可读取。")
    actors = {row["id"]:row["name"] for row in master["actors"]}
    realms = {row["id"]:row.get("server") for row in master["actors"]}
    abilities = {row["gameID"]:row["name"] for row in master["abilities"]}
    friendlies = set(fight["friendlyPlayers"] or [])
    seconds = (fight["endTime"] - fight["startTime"]) / 1000
    metrics = []
    spell_ids = set()
    for field, label, rate in (("damage", "伤害输出", "DPS"), ("healing", "治疗输出", "HPS")):
        rows = []
        entries = ((report.get(field) or {}).get("data") or {}).get("entries")
        if not isinstance(entries, list):
            raise wcl.WclError("该场次的输出统计暂不可用。")
        for entry in entries:
            if entry.get("id") not in friendlies or entry.get("total") is None:
                continue
            total = entry["total"]
            skills = sorted(entry.get("abilities") or [], key=lambda s:s.get("total",0), reverse=True)
            spell_ids.update(s["guid"] for s in skills if s.get("guid",0)>0)
            rows.append(dict(name=entry["name"], realm=realms.get(entry["id"]), actor=entry["id"], total=total, rate=round(total/seconds,1) if seconds else None,
                             skills=skills))
        metrics.append((label,rate,sorted(rows,key=lambda r:r["total"], reverse=True)))
    deaths = []
    for e in report["deaths"]["data"]:
        if e.get("targetID") not in friendlies or e.get("fake"):
            continue
        sid = e.get("killingAbilityGameID")
        if sid:
            spell_ids.add(sid)
        deaths.append(dict(sec=round((e["timestamp"]-fight["startTime"])/1000,2), target=actors.get(e["targetID"], "未知玩家"),
                           killer=actors.get(e.get("killerID")), sid=sid))
    zh = localization.spell_names({sid:abilities.get(sid,"") for sid in spell_ids})
    for _,_,rows in metrics:
        for row in rows:
            for s in row["skills"]:
                s["label"] = zh.get(s.get("guid")) or s.get("name") or s.get("guid")
    for row in deaths:
        row["skill"] = zh.get(row["sid"]) or abilities.get(row["sid"]) or row["sid"]
    return dict(metrics=metrics, deaths=deaths, limited=bool(report["deaths"]["nextPageTimestamp"]))


def fetch_detail(binding, fight):
    variables = dict(code=fight["code"],ids=[fight["id"]],start=fight["startTime"],end=fight["endTime"])
    raw = wcl.read(binding,wcl.FIGHT_DETAIL,variables)["reportData"]["report"]
    if not raw:
        raise wcl.WclError("这场战斗的事件明细暂不可用。")
    return details(raw, fight)


def compare_players(a, b, left, right):
    tables = []
    for (label,rate,first), (_,_,second) in zip(a["metrics"], b["metrics"]):
        def keyed(rows, fight):
            return {(r["name"],r["realm"] or (fight["code"],r["actor"])):r for r in rows}
        lrows, rrows = keyed(first,left), keyed(second,right)
        cells = []
        for key in lrows.keys() | rrows.keys():
            l, r = lrows.get(key,{}).get("rate"), rrows.get(key,{}).get("rate")
            delta = round(r-l,1) if l is not None and r is not None else None
            cells.append(dict(name=key[0], realm=(lrows.get(key) or rrows[key])["realm"],left=l,right=r,
                              delta=f"{delta:+,.1f}" if delta is not None else "—",
                              tone=("up" if delta>0 else "down") if delta else "", gap=abs(delta or 0)))
        tables.append((label,rate,sorted(cells,key=lambda x:(-x["gap"],x["name"]))))
    return tables


@endpoint("GET")
def report(request, code=None):
    q = request.GET
    codes = list(dict.fromkeys(([code] if code else []) + q.getlist("also")))
    if len(codes)>20 or any(not re.fullmatch(r"[a-zA-Z0-9]{16}", c) for c in codes):
        raise Http404
    private = q.get("channel") == "private"
    search = QueryDict(q.get("origin",""))
    origin = search_context(search)
    listing = listing_url(origin,private)
    binding = binding_for(request.user) if private else None
    ctx = dict(section="report",private=private,binding=binding,listing_url=listing)
    status = 200
    try:
        if not codes:
            raise ValueError("请先在报告列表勾选要分析的报告。")
        if private and not binding:
            raise wcl.AccessDenied("请先进入私有报告渠道授权 WCL 账号。")
        manual = not code or q.get("selection") == "1"
        can_discover = not manual and len(codes) == 1 and bool(
            (private and search.get("channel") == "private") or (not private and search.get("name"))
        )
        if can_discover and q.get("discover") == "1" and q.get("group"):
            ctx.update(directory_url=workspace_url(codes, private, origin, focus=q["group"]),
                       single_review_url=workspace_url(codes, private, origin, group=q["group"]))
            page = int(search.get("page", "1"))
            if not 1 <= page <= 100000:
                raise ValueError("报告页码无效。")
            if private:
                result = wcl.read(binding,wcl.REPORTS,{"user":binding.wcl_id,"page":page})["reportData"]["reports"]
            else:
                result = public_listing(request, {}, page, search)
            related = [r["code"] for r in (result or {}).get("data", [])
                       if any(catalog.group_key(r, f) == q["group"] for f in r.get("fights") or [])]
            scope_codes = list(dict.fromkeys([codes[0], *related]))[:20]
            return redirect(workspace_url(scope_codes, private, origin, group=q["group"], from_log=1))
        combat_loading.message(f"正在向 WCL 请求 {len(codes)} 份报告的战斗目录。")
        reports = catalog.fetch_reports(codes,binding)
        combat_loading.message("战斗目录已收到，正在按首领和难度整理场次。")
        groups = catalog.attempts(reports)
        expanded = q.get("from_log") == "1"
        def link(**params):
            return workspace_url(codes, private, origin, from_log=1 if expanded else None,
                                 selection=1 if manual else None, back_catalog=q.get("back_catalog"), **params)
        def directory(focus=None):
            if q.get("back_catalog"):
                from botend.dashboard.combat_library import back_directory
                return back_directory(q["back_catalog"])
            return workspace_url(codes[:1] if expanded else codes, private, origin,
                                 selection=1 if manual else None, focus=focus)
        ctx.update(reports=reports,groups=groups,code=codes[0],directory_url=directory(),title=reports[0]["title_label"],
                   form_url=reverse("combat_wcl_report",args=[codes[0]]))
        for g in groups:
            g["url"] = link(group=g["key"])
            g["focus_url"] = directory(g["key"])
            for p in g["pulls"]:
                p["url"] = single_url(p["code"],p["id"],private,origin,q.get("back_catalog"))
        ref = q.get("pull")
        if q.get("fight"):
            fid = q["fight"]
            if fid == "last" and reports[0]["fights"]:
                fid = reports[0]["fights"][-1]["id"]
            ref = f"{codes[0]}.{fid}"
        keyed = {v["ref"]:(g,p,v) for g in groups for p in g["pulls"] for v in p["views"]}
        key = q.get("group")
        if ref and (ref not in keyed or (key and keyed[ref][0]["key"] != key)):
            raise ValueError("所选场次不在当前首领和难度范围内。")
        group = next((g for g in groups if g["key"] == key),None)
        if key and group is None:
            raise ValueError("所选报告中没有这个首领和难度。")
        if ref:
            group = keyed[ref][0]
            if group["dungeon"] or q.get("compare") != "1":
                selected = keyed[ref][2]
                return redirect(single_url(selected["code"],selected["id"],private,origin,q.get("back_catalog")))
        if group and group["dungeon"]:
            return redirect(directory(group["key"]))
        if not group:
            active = next((g for g in groups if g["key"] == q.get("focus")), groups[0] if groups else None)
            pick = q.get("pick")
            if pick == "last" and reports[0]["fights"]:
                pick = reports[0]["fights"][-1]["id"]
            picked = keyed.get(f"{codes[0]}.{pick}")
            if picked:
                active = picked[0]
                ctx["picked"] = picked[1]["ref"]
            ctx.update(show_directory=True, active=active, report_count=len(reports),
                       pull_count=sum(len(g["pulls"]) for g in groups))
            for g in groups:
                g["discover"] = can_discover and not g["dungeon"]
                g["review_url"] = link(group=g["key"], discover=1 if g["discover"] else None)
                for p in g["pulls"]:
                    minutes, seconds = divmod(int(p["duration"]), 60)
                    p["elapsed"] = f"{minutes}:{seconds:02d}"
        if group:
            pulls = group["pulls"]
            scope = group["key"]
            ctx.update(group=group,directory_url=directory(scope),group_url=link(group=scope),
                       compare_url=link(group=scope,compare="1",pull=ref))
            ctx["scope_fields"] = [("also",c) for c in codes[1:]] + [("group",scope)]
            if q.get("back_catalog"):
                ctx["scope_fields"].append(("back_catalog",q["back_catalog"]))
            if expanded:
                ctx["scope_fields"].append(("from_log","1"))
            if manual:
                ctx["scope_fields"].append(("selection","1"))
            if origin:
                ctx["scope_fields"].append(("origin",origin))
            if private:
                ctx["scope_fields"].append(("channel","private"))
            curve = q.get("curve","remaining")
            if curve not in ("remaining","boss"):
                raise ValueError("请选择有效的推进指标。")
            ctx.update(curve=curve,hp_url=link(group=scope,curve="boss"),progress_url=link(group=scope))
            datafield = "fightPercentage" if curve=="remaining" else "bossPercentage"
            points = [dict(id=p["number"], start=p["absolute_start"],end=p["absolute_end"],pct=p[datafield],
                           stage_label=f"第 {p['lastPhase']} 阶段" if p["lastPhase"] else "阶段未知")
                      for p in pulls if p[datafield] is not None and p["absolute_end"]>p["absolute_start"]]
            if points:
                points.sort(key=lambda p:p["end"])
                ctx["chart"] = progress_chart(points,"pct",keyed[ref][1]["number"] if ref else None,
                                              lambda pull,page:pulls[pull-1]["url"])
            ctx["comparing"] = q.get("compare") == "1"
            if ref:
                _, canonical, selected = keyed[ref]
                other = keyed.get(q.get("against"))
                if not other or other[0] is not group or other[1] is canonical:
                    raise ValueError("请选择同一首领组内另一次独立尝试进行对比。")
                ctx.update(fight=selected,number=canonical["number"],code=selected["code"],ref=canonical["ref"])
                combat_loading.message("正在读取第一场的伤害、治疗和死亡记录。")
                info = fetch_detail(binding,selected)
                target = other[2]
                combat_loading.message("第一场明细已收到，正在读取对照场次。")
                b = fetch_detail(binding,target)
                ctx.update(deaths=info["deaths"],other=other[1],other_info=b,against=other[1]["ref"],
                           comparisons=compare_players(info,b,selected,target))
    except (ValueError, wcl.WclError) as exc:
        ctx["error"] = str(exc)
        status = exc.status if isinstance(exc, wcl.WclError) else 400
    return render(request,"dashboard/combat_wcl.html",ctx,status=status)
