"""团队分析册、同口径多场查询和有行号依据的复盘页面。"""

from urllib.parse import urlencode
import zlib

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from botend.dashboard.combat_logs import endpoint
from botend.models import (
    CombatLogCollection,
    CombatLogMechanicReview,
    CombatLogReport,
    CombatLogReview,
)
from botend.services import combat_log_actor, combat_log_mechanics, combat_log_duel, combat_log_replay
from botend.services import combat_log_benchmark, combat_public_events
from botend.services import combat_wcl_index
from botend.services import combat_log_analysis as analysis
from botend.services import combat_log_visuals as visuals
from botend.services import combat_boss_comparison as boss_compare
from botend.services import combat_log_archive as archive
from botend.services import combat_log_reviews as reviews
from botend.services import combat_replay_activity, combat_replay_maps
from requests import RequestException

FILTERS = (
    "panel",
    "cross",
    "kind",
    "side",
    "seriesgroup",
    "file",
    "cohort",
    "metric",
    "group",
    "start",
    "end",
    "source",
    "target",
    "ability",
    "anchor",
    "occurrence",
    "before",
    "beyond",
    "rule",
    "spec",
    "role",
    "pets",
)


def collections(user):
    return CombatLogCollection.objects.filter(
        Q(owner=user) | Q(readers=user)
    ).distinct()


def visible(user):
    return CombatLogReport.objects.filter(
        Q(owner=user) | Q(collections__in=collections(user))
    ).filter(expires_at__gt=timezone.now()).distinct()


def readable_report(user, pk):
    report = visible(user).filter(pk=pk, published__isnull=False).first()
    if report is None:
        report = combat_wcl_index.resolve(user, pk)
    if report is None:
        raise Http404
    return report


def selected_reports(user, params):
    ids = list(dict.fromkeys(params.getlist("log")))
    if not ids:
        return []
    authorized = visible(user).filter(published__isnull=False).in_bulk(ids)
    by_id = {str(pk): report for pk, report in authorized.items()}
    for key in ids:
        if key not in by_id:
            by_id[key] = readable_report(user,key)
    return [by_id[key] for key in ids]


def source_context(reports):
    fields = [("log", str(r.pk)) for r in reports]
    query = urlencode(fields)
    base = reverse("combat_workspace") + "?" + query
    return dict(scope_logs=reports, scope_fields=fields, battle_url=base,
                players_url=base + "&mode=players", change_url=reverse("combat_logs") + "?" + query)


def comparison_sources(user):
    return visible(user).filter(published__isnull=False).order_by("created_at", "pk")


def player_filters(params):
    fields = ("left", "right", "left_file", "right_file", "left_fight", "right_fight",
              "metric", "cohort", "detail", "breakdown", "measure", "scale", "pairing", "target_group", "run")
    return {key: params[key] for key in fields if key in params}


def link_players(ctx, params):
    battle = params.copy()
    for key in ("log", "mode", "battle", "duel", "back_catalog"):
        battle.pop(key, None)
    query = player_filters(QueryDict(params.get("duel", "")))
    if params.get("back_catalog"):
        query["back_catalog"] = params["back_catalog"]
    if battle:
        query["battle"] = battle.urlencode()
    if query:
        ctx["players_url"] += "&" + urlencode(query)


@endpoint("GET")
def workspace(request):
    reports = selected_reports(request.user, request.GET)
    if request.GET.get("mode") == "boss":
        ctx = source_context(reports)
        ctx["battle_url"] = reverse("combat_workspace") + "?" + urlencode([("mode", "boss"), *ctx["scope_fields"]])
        ctx.update(view="selection", multi_scope=True, boss_discovery=True)
        return render_analysis(request, reports or comparison_sources(request.user), ctx)
    if not reports:
        if request.GET.get("mode") == "players":
            return render_players(request, comparison_sources(request.user), {})
        return redirect("combat_logs")
    ctx = source_context(reports)
    if request.GET.get("mode") == "players":
        return render_players(request, reports, ctx)
    current = request.GET.get("sample")
    if current or len(reports) == 1:
        report = next((r for r in reports if str(r.pk) == current), None) if current else reports[0]
        if not report:
            raise Http404
        ctx.update(view="single", report=report)
        return render_analysis(request, [report], ctx, selected=analysis.nonnegative(request.GET, "fight"))
    ctx.update(view="selection", reports=reports, multi_scope=True)
    return render_analysis(request, reports, ctx)


@endpoint("POST")
def rebuild(request, pk):
    report = get_object_or_404(
        CombatLogReport, pk=pk, owner=request.user, published__isnull=False
    )
    archive.enqueue(request.user, report.pk, rebuild=True)
    return redirect("combat_report", pk=pk)


@endpoint("GET", "POST")
def collection_list(request):
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        if not 1 <= len(title) <= 100:
            raise ValueError("请输入 1 至 100 字的分析册名称。")
        book = CombatLogCollection.objects.create(owner=request.user, title=title)
        return redirect("combat_collection", pk=book.pk)
    return redirect(reverse("combat_logs") + "#saved-collections")


@endpoint("GET", "POST")
def collection(request, pk):
    book = get_object_or_404(collections(request.user), pk=pk)
    action = request.POST.get("action")
    if action == "generate-rule" and (
        book.owner_id != request.user.pk or not request.GET.get("rule")
    ):
        raise Http404
    if request.method == "POST" and action != "generate-rule":
        with transaction.atomic():
            book = get_object_or_404(
                CombatLogCollection.objects.select_for_update(), pk=book.pk
            )
            if not collections(request.user).filter(pk=book.pk).exists():
                raise Http404
            if action == "attach":
                report = get_object_or_404(
                    CombatLogReport,
                    pk=request.POST.get("report"),
                    owner=request.user,
                    published__isnull=False,
                    expires_at__gt=timezone.now(),
                )
                book.reports.add(report)
            elif action == "detach":
                report = get_object_or_404(book.reports, pk=request.POST.get("report"))
                if request.user.pk not in (book.owner_id, report.owner_id):
                    raise Http404
                book.reports.remove(report)
            elif action in ("invite", "revoke") and book.owner_id == request.user.pk:
                member = get_object_or_404(
                    get_user_model(),
                    username=request.POST.get("username"),
                    is_active=True,
                )
                if member.pk != book.owner_id:
                    if action == "invite":
                        book.readers.add(member)
                    else:
                        book.readers.remove(member)
                        book.reports.remove(*book.reports.filter(owner=member))
            elif action in ("bookmark", "rule") and book.owner_id == request.user.pk:
                name = request.POST.get("name", "").strip()
                if not 1 <= len(name) <= 100:
                    raise ValueError("名称应在 1–100 字之间。")
                filters = {k: request.GET[k] for k in FILTERS if request.GET.get(k)}
                if request.GET.getlist("series"):
                    filters["series"] = request.GET.getlist("series")
                if request.GET.getlist("attempt"):
                    filters["attempt"] = request.GET.getlist("attempt")
                _, cohorts, _ = analysis.catalog(
                    book.reports.filter(expires_at__gt=timezone.now()).order_by("created_at", "pk")
                )
                if filters.get("cohort") not in cohorts:
                    raise ValueError("请先选择一组可比较的活动。")
                if action == "bookmark":
                    book.bookmarks.create(name=name, filters=filters)
                else:
                    trigger = analysis.nonnegative(request.POST, "trigger")
                    response = analysis.nonnegative(request.POST, "response")
                    window = analysis.nonnegative(request.POST, "window", 8)
                    kind = request.POST.get("kind")
                    if (
                        kind not in ("impact", "dispel")
                        or not trigger
                        or not 1 <= window <= 120
                        or (kind == "impact" and not response)
                    ):
                        raise ValueError(
                            "请输入有效的触发和响应技能，观察窗口为 1–120 秒。"
                        )
                    rule = book.mechanics.create(
                        name=name,
                        cohort=filters["cohort"],
                        kind=kind,
                        trigger=str(trigger),
                        response=str(response or trigger),
                        window=window,
                    )
                    filters["rule"] = rule.pk
                return redirect(
                    reverse("combat_collection", args=[pk])
                    + "?"
                    + urlencode(filters, doseq=True)
                )
            else:
                raise ValueError("当前账号不能执行此操作。")
        return redirect("combat_collection", pk=book.pk)
    reports = list(book.reports.filter(expires_at__gt=timezone.now()).order_by("created_at", "pk"))
    bookmarks = [
        dict(name=b.name, url="?" + urlencode(b.filters, doseq=True))
        for b in book.bookmarks.order_by("pk")
    ]
    ctx = {
        **source_context(reports),
        "view": "book",
        "book": book,
        "reports": reports,
        "bookmarks": bookmarks,
        "readers": book.readers.order_by("username"),
        "available": CombatLogReport.objects.filter(
            owner=request.user, published__isnull=False, expires_at__gt=timezone.now()
        ).exclude(collections=book),
    }
    ctx["battle_url"] = reverse("combat_collection", args=[book.pk])
    ctx["players_url"] += "&" + urlencode({"book": book.pk})
    return render_analysis(request, reports, ctx)


def report_navigation(params, activities, selected, ctx):
    """保留跨场通用的查询条件，场内波次和分页只属于当前场。"""
    carried = params.copy()
    if activities:
        carried["cohort"] = activities[0]["cohort"]
    for key in ("fight", "wave", "after", "page", "colpage", "eventpage", "rule"):
        carried.pop(key, None)
    parts = set(params.getlist("part"))
    sequence = [a for a in activities if not parts or str(a["id"]) in parts]
    if parts - {str(a["id"]) for a in activities}:
        raise ValueError("所选场次已变化，请从日志目录重新选择。")
    ctx["activities"] = activities
    ctx["combined_url"] = "?" + carried.urlencode()
    ctx["sequence_hidden"] = [
        (k, v) for k, values in carried.lists() if k != "part" for v in values
    ]
    for a in activities:
        q = carried.copy()
        a["checked"] = str(a["id"]) in parts
        if parts and not a["checked"]:
            q.pop("part", None)
        q["fight"] = a["id"]
        a["url"] = "?" + q.urlencode()
    if selected is not None:
        positions = [i for i, a in enumerate(sequence) if a["id"] == selected]
        if not positions:
            raise Http404
        pos = positions[0]
        ctx.update(
            position=pos + 1,
            sequence_count=len(sequence),
            previous_fight=sequence[pos - 1] if pos else None,
            next_fight=sequence[pos + 1] if pos + 1 < len(sequence) else None,
        )
        ctx["selected"] = sequence[pos]
        return [sequence[pos]]
    ids = {a["id"] for a in sequence}
    included = [a for a in sequence if a["parent"] not in ids]
    ctx.update(combined=True, covered=len(sequence) - len(included))
    return included


def single_fight_url(sample, ctx, params):
    fields = list(ctx.get("scope_fields", ()))
    if fields:
        path = reverse("combat_workspace")
        fields.append(("sample", str(sample["report"].pk)))
    else:
        path = reverse("combat_analysis", args=[sample["report"].pk])
    fields.append(("fight", sample["id"]))
    for key in ("back_catalog", "origin", "duel", "metric"):
        if params.get(key):
            fields.append((key, params[key]))
    return path + "?" + urlencode(fields)


def restore_directory(ctx, params):
    if params.get("back_catalog"):
        from botend.dashboard.combat_library import back_directory
        directory = back_directory(params["back_catalog"])
        ctx["directory_url"] = directory
        ctx["change_url"] = reverse("combat_logs") + "?" + directory.partition("?")[2]


def render_analysis(request, reports, ctx, selected=None):
    from botend.services.combat_loading import message
    message("正在整理所选战斗的场次、波次和统计数据。")
    params = request.GET
    restore_directory(ctx, params)
    if ctx.get("scope_logs"):
        link_players(ctx, params)
    if ("book" in ctx or ctx.get("multi_scope")) and params.get("file"):
        reports = [r for r in reports if str(r.pk) == params["file"]]
        if not reports:
            raise Http404
    samples, cohorts, skipped = analysis.catalog(reports)
    if ctx.get("boss_discovery"):
        samples = [s for s in samples if s["raid"] and s["game_id"] not in ("", "0")]
        cohorts = {s["cohort"]: cohorts[s["cohort"]] for s in samples}
    only_activity = len(samples) == 1
    cohort = params.get("cohort", "")
    if "report" in ctx:
        parts = set(params.getlist("part"))
        selected_rows = [s for s in samples if str(s["id"]) in parts or s["id"] == selected]
        if parts - {str(s["id"]) for s in selected_rows} or selected is not None and not any(s["id"] == selected for s in selected_rows):
            raise Http404
        targets = {s["cohort"] for s in selected_rows}
        if len(targets) > 1 or cohort and targets and targets != {cohort}:
            raise ValueError("只能合并同一副本或同一首领的场次。")
        if not targets and not cohort:
            if params.keys() - {"log", "sample"}:
                targets = {s["cohort"] for s in samples if not s["parent"]}
            else:
                targets = set(cohorts)
        if not cohort and len(targets) == 1:
            cohort = next(iter(targets))
        if cohort:
            if cohort not in cohorts:
                raise Http404
            eligible = [s for s in samples if s["cohort"] == cohort]
            included_ids = {a["id"] for a in eligible}
            ctx["nested_count"] = sum(s["parent"] in included_ids for s in samples)
            samples = eligible
        elif samples:
            ctx.update(view="selection", multi_scope=True, reports=reports)
            ctx.pop("report")
    if "report" in ctx:
        if selected is None and len(samples) == 1 and (only_activity or params.get("cohort")) and not parts:
            selected = samples[0]["id"]
        samples = report_navigation(params, samples, selected, ctx)
        ctx["covered"] = ctx.get("covered", 0) + ctx.get("nested_count", 0)
    elif cohort:
        if cohort not in cohorts:
            raise ValueError("该活动集合已发生变化，请重新选择。")
        samples = [s for s in samples if s["cohort"] == cohort]
        wanted = set(params.getlist("attempt"))
        choices = []
        chosen = []
        for sample in samples:
            key = f"{sample['report'].pk}.{sample['start_line']}"
            sample["url"] = single_fight_url(sample, ctx, params)
            choices.append({"key": key, "sample": sample, "checked": key in wanted})
            if key in wanted:
                chosen.append(sample)
        if wanted and len(chosen) != len(wanted):
            skipped.append(
                f"{len(wanted) - len(chosen)} 个指定场次已不在当前日志视角或授权范围内。"
            )
        ctx["attempt_choices"] = choices
        if wanted:
            samples = chosen
    ctx.update(cohorts=cohorts, cohort=cohort, cohort_label=cohorts.get(cohort), skipped=skipped)
    if cohort and ctx.get("scope_logs") and not params.get("duel"):
        comparison_scope = {"target_group": cohort}
        if selected is not None and ctx.get("report"):
            comparison_scope["run"] = f"{ctx['report'].pk}.{selected}"
        ctx["players_url"] += "&" + urlencode(comparison_scope)
    if ("book" in ctx or ctx.get("multi_scope")) and not cohort:
        samples = [s for s in samples if not s["dungeon_detail"]]
        ctx["cohorts"] = {s["cohort"]: cohorts[s["cohort"]] for s in samples}
        ctx["overview"], ctx["excluded"] = analysis.overview(samples)
        by_cohort = {}
        for sample in samples:
            sample["url"] = single_fight_url(sample, ctx, params)
            by_cohort.setdefault(sample["cohort"], []).append(sample)
        for entry in ctx["overview"]:
            entry["fights"] = by_cohort[entry["cohort"]]
            entry["label"] = cohorts[entry["cohort"]]
        if ctx.get("multi_scope") and ctx.get("scope_fields"):
            for entry in ctx["overview"]:
                entry["url"] += "&" + urlencode(ctx["scope_fields"])
        if params.get("file"):
            for entry in ctx["overview"]:
                entry["url"] += "&" + urlencode({"file": params["file"]})
        for entry in ctx["overview"]:
            if entry["raid"] and (entry["count"] > 1 or ctx.get("boss_discovery")):
                entry["boss_url"] = entry["url"] + "&panel=boss"
                if ctx.get("boss_discovery"):
                    entry["boss_url"] += "&mode=boss"
    if "report" in ctx or cohort:
        panel = params.get("panel", "table")
        if samples and all(s["raid"] and s["game_id"] not in ("", "0") for s in samples) and cohort:
            ctx["boss_url"] = boss_compare.link(params, panel="boss", rule=None, wave=None)
            if ctx.get("wcl_progression") and selected == ctx["report"].wcl_source["fight"]:
                ctx["boss_url"] = ctx["wcl_progression"]
                if panel == "boss":
                    return redirect(ctx["boss_url"])
            ctx["all_boss_url"] = reverse("combat_workspace") + "?" + urlencode(
                {"mode": "boss", "cohort": cohort, "panel": "boss"})
        if panel in ("boss", "dungeon_boss"):
            dungeon_compare = panel == "dungeon_boss"
            if dungeon_compare:
                if not cohort or not samples or any(not s["parent"] for s in samples):
                    raise ValueError("请选择同一副本中归属明确的首领。")
                samples = [s for s in samples if s["comparable_key_boss"]]
                ctx["dungeon_compare"] = True
                ctx["attempt_choices"] = [c for c in ctx.get("attempt_choices", ()) if c["sample"]["comparable_key_boss"]]
            elif not ctx.get("boss_url"):
                raise ValueError("首领开荒复盘适用于同一团本首领。大秘境首领可在所属钥匙的分析波次中查看。")
            ctx["data_url"] = boss_compare.link(params, panel="table", at=None, base=None, behavior=None,
                                                batch=None, bracket=None)
            for sample in samples:
                sample["url"] = single_fight_url(sample, ctx, params)
                if dungeon_compare:
                    sample["url"] = single_fight_url(dict(sample, id=sample["parent"]), ctx, params) + "&" + urlencode({"wave": -sample["id"]})
            ctx.update(boss_compare.comparison(samples, params))
            return render(request, "dashboard/combat_boss_comparison.html", ctx)
        if panel not in ("table", "pivot", "events", "review", "pick"):
            raise ValueError("该数据视图不存在。")
        query = params.copy()
        query["panel"] = "review" if params.get("rule") else panel
        # 从普通单场入口先看第一波，带有既有分析条件的深链接沿用其原范围。
        ctx.update(analysis.run(samples, query, single=selected is not None,
                                first_wave=not (params.keys() - {"fight", "part", "log", "sample", "duel", "cohort", "origin","back_catalog"})))
        if ctx["active_wave"] and not params.get("wave"):
            params = params.copy()
            params["wave"] = str(ctx["active_wave"]["id"])
        ctx["replay_back"] = params.urlencode()
        actor_links = {}
        for player in ctx["players"]:
            url = reverse("combat_actor", args=[ctx["report"].pk, selected, player["guid"]])
            attempt = ctx["comparisons"][0]
            scope = {"back": ctx["replay_back"], "from_ms": attempt["window_start"] - attempt["start_ms"]}
            if attempt["window_end"] is not None:
                scope["to_ms"] = attempt["window_end"] - attempt["start_ms"]
            player["actor_url"] = url + "?" + urlencode(scope)
            actor_links[player["guid"]] = player["actor_url"]
        ctx["panel"] = panel

        def link(**changes):
            q = params.copy()
            for transient in ("after", "page", "eventpage", "colpage", "term"):
                q.pop(transient, None)
            if "group" in changes and changes["group"] != ctx["group"]:
                q.pop("series", None)
                q.pop("seriesgroup", None)
            for k, v in changes.items():
                if v is None:
                    q.pop(k, None)
                elif isinstance(v, list):
                    q.setlist(k, v)
                else:
                    q[k] = v
            return "?" + q.urlencode()

        axis = ctx["group"]
        ctx["metric_label"] = analysis.METRICS[ctx["metric"]]
        ctx["metric_tabs"] = [
            (
                name,
                link(
                    metric=key,
                    cross=None,
                    panel="table" if panel == "pick" else panel,
                    group=params.get("group"),
                ),
                key == ctx["metric"],
                key,
            )
            for key, name in analysis.METRICS.items()
        ]
        ctx["dimension_tabs"] = [
            (name, link(group=key, cross=None), key == axis)
            for key, name in analysis.GROUPS.items()
        ]
        ctx["column_tabs"] = [
            (name, link(cross=key), key == ctx["cross"])
            for key, name in analysis.GROUPS.items()
            if key != axis
        ]
        ctx["panel_tabs"] = [
            (name, link(panel=key, rule=None), key == panel)
            for key, name in (
                ("table", "明细表"),
                ("pivot", "交叉表"),
                ("events", "事件流"),
                ("review", "复盘与机制"),
            )
        ]
        active_dimensions = {}
        ctx["chips"] = []
        for key in (*analysis.DIMENSIONS, "anchor", "start", "end", "pets", "side"):
            if not params.get(key):
                continue
            name = (
                analysis.GROUPS.get(key)
                or {
                    "anchor": "对齐技能",
                    "start": "起始秒",
                    "end": "结束秒",
                    "pets": "召唤物",
                    "side": "统计单位",
                }[key]
            )
            display = params[key]
            if key == "pets":
                display = {"players": "仅玩家", "owner": "归入主人", "separate": "独立列出"}.get(display, display)
            elif key == "side":
                display = {"players": "玩家", "npc": "NPC", "all": "全部单位"}.get(display, display)
            for r in reports:
                if (
                    key not in ("source", "target", "ability", "spec")
                    or not r.published
                ):
                    break
                table, field, label = {
                    "source": ("actor", "guid", "name"),
                    "target": ("actor", "guid", "name"),
                    "ability": ("ability", "id", "name"),
                    "spec": ("spec", "id", "label"),
                }[key]
                if r.summary.get("index_version", 0) < analysis.VERSION:
                    continue
                with analysis.database(r) as db:
                    match = db.execute(
                        f"SELECT {label} FROM {table} WHERE {field}=?", (display,)
                    ).fetchone()
                if match:
                    display = match[0]
                    break
            remove = link(**{key: None})
            if key in analysis.DIMENSIONS:
                active_dimensions[key] = (display, remove)
            else:
                ctx["chips"].append((name, display, remove))
        ctx["pickers"] = [
            (key, name, link(**{key: None}, panel="pick", group=key, cross=None),
             active_dimensions.get(key))
            for key, name in analysis.GROUPS.items()
        ]
        term = params.get("term", "").strip().casefold()
        coverage_table = ctx["metric"] == "auras" and panel == "table"
        if coverage_table:
            ctx["buff_spell_count"] = len({r["spell"] for r in ctx["coverage"]})
            if len(ctx["comparisons"]) == 1:
                attempt = ctx["comparisons"][0]
                if attempt["window_end"] is not None:
                    ctx["buff_window_seconds"] = max(0, attempt["window_end"] - attempt["window_start"]) / 1000
            ctx["coverage"].sort(key=lambda r: (-r["percent"], r["target"], r["name"], r["fight"]))
            if term:
                ctx["coverage"] = [r for r in ctx["coverage"] if any(
                    term in str(r[key]).casefold() for key in ("name", "target", "spell", "filename"))]
        if term:
            ctx["rows"] = [
                r
                for r in ctx["rows"]
                if term in str(r["name"]).casefold()
                or term in str(r["ident"]).casefold()
            ]
        page = max(1, analysis.nonnegative(params, "page", 1))
        count = len(ctx["coverage"] if coverage_table else ctx["rows"])
        ctx["groups_count"] = count
        page = min(page, max(1, (count + 39) // 40))
        ctx["rows"] = ctx["rows"][(page - 1) * 40 : page * 40]
        ctx["coverage"] = ctx["coverage"][(page - 1) * 40 : page * 40]
        for row in ctx["coverage"]:
            row["target_url"] = link(target=row["guid"])
            row["ability_url"] = link(ability=row["spell"])
        ctx["icon_sources"] = [reverse("combat_spell_icons", args=[pk]) for pk in
                               dict.fromkeys(c["report"].pk for c in ctx["comparisons"])]
        if page > 1:
            ctx["previous_groups"] = link(page=page - 1, term=term)
        if page * 40 < count:
            ctx["next_groups"] = link(page=page + 1, term=term)
        if panel == "pick" and request.headers.get("X-Combat-View") == "choices":
            visuals.decorate(ctx)
            return JsonResponse({
                "items": [{"id": r["ident"], "name": r["name"], "hits": r["hits"], "icon": r.get("icon", ""), "icon_id": r.get("icon_id"), "icon_label": r.get("icon_label", "")} for r in ctx["rows"]],
                "count": count, "previous": ctx.get("previous_groups"), "next": ctx.get("next_groups"),
            })
        headers = ctx["headers"]
        colpage = max(
            1, min(analysis.nonnegative(params, "colpage", 1), (len(headers) + 7) // 8)
        )
        ctx["headers"] = headers[(colpage - 1) * 8 : colpage * 8]
        visuals.decorate(ctx)
        if colpage > 1:
            ctx["prev_cols"] = link(colpage=colpage - 1, page=page, term=term)
        if colpage * 8 < len(headers):
            ctx["next_cols"] = link(colpage=colpage + 1, page=page, term=term)
        series = [c["ident"] for c in ctx["curves"]]
        for curve in ctx["curves"]:
            curve["remove"] = link(
                series=[k for k in series if k != curve["ident"]], seriesgroup=axis
            )
        for item in ctx["rows"]:
            ident = str(item["ident"])
            if axis in ("source", "target"):
                item["actor_url"] = actor_links.get(ident)
            if ident not in series and len(series) < 4:
                item["plot_url"] = link(
                    series=[*series, ident], seriesgroup=axis, page=page, term=term
                )
            other = "ability" if axis != "ability" else "target"
            item["url"] = link(
                **{axis: item["ident"]}, group=other, cross=None, panel="table"
            )
            item["cells"] = []
            for header in ctx["headers"]:
                cell = ctx["cells"].get((item["ident"], header["ident"]))
                item["cells"].append(
                    dict(
                        cell,
                        url=link(
                            **{axis: item["ident"], ctx["cross"]: header["ident"]},
                            panel="events",
                        ),
                    )
                    if cell
                    else None
                )
        eventpage = max(1, analysis.nonnegative(params, "eventpage", 1))
        if eventpage > 1:
            ctx["prev_events"] = link(eventpage=eventpage - 1)
        if eventpage * 50 < ctx["matched_hits"]:
            ctx["next_events"] = link(eventpage=eventpage + 1)
        ctx["hidden_query"] = [
            (k, v)
            for k, values in params.lists()
            for v in values
            if k not in ("term", "page", "colpage", "after", "eventpage")
        ]
        if params.get("start") or params.get("end"):
            ctx["full_time_url"] = link(start=None, end=None)
        if ctx["trend"]:
            ctx["plot_peak"] = max(bucket["value"] for bucket in ctx["trend"])
            ctx["plot_end"] = ctx["trend"][-1]["seconds"] + ctx["step"]
        for bucket in ctx["trend"]:
            start = bucket["seconds"]
            if not params.get("anchor"):
                lo = max(start, analysis.nonnegative(params, "start", 0))
                hi = min(
                    start + ctx["step"],
                    analysis.nonnegative(params, "end", start + ctx["step"]),
                )
                bucket["url"] = link(start=lo, end=hi) if hi > lo else None
        preceding = {}
        peak_rate = max((r["rate"] or 0 for r in ctx["comparisons"]), default=0)
        for item in ctx["comparisons"]:
            if ctx.get("combined"):
                rate = item["rate"]
                prior = preceding.get(item["cohort"])
                item["change"] = (
                    rate - prior if rate is not None and prior is not None else None
                )
                item["relative_height"] = (
                    100 * rate / peak_rate if peak_rate and rate is not None else 0
                )
                preceding[item["cohort"]] = rate
                continue
            item["url"] = single_fight_url(item, ctx, params)
        if ctx["waves"]:
            ctx["wave_fields"] = [(k,v) for k,v in ctx["hidden_query"]
                                  if k not in ("wave","start","end","anchor","occurrence","before","beyond")]
        if ctx["next_line"]:
            ctx["next_url"] = link(after=ctx["next_line"])
        ctx["reset_url"] = link(
            source=None,
            target=None,
            ability=None,
            spec=None,
            role=None,
            start=None,
            end=None,
            wave=params.get("wave"),
            anchor=None,
            occurrence=None,
            before=None,
            beyond=None,
        )
        if "book" in ctx:
            rules = ctx["book"].mechanics.filter(cohort=cohort).order_by("pk")
            ctx["custom_rules"] = [{"rule": r, "url": link(rule=r.pk)} for r in rules]
            if params.get("rule"):
                rule = get_object_or_404(rules, pk=analysis.nonnegative(params, "rule"))
                ctx["matrix"] = combat_log_mechanics.evaluate(rule, ctx["comparisons"])
                packet, refs, fingerprint = combat_log_mechanics.review_packet(
                    ctx["matrix"], ctx["comparisons"]
                )
                key = dict(mechanic=rule, fingerprint=fingerprint)
                saved = CombatLogMechanicReview.objects.filter(**key).first()
                notes = saved.findings if saved else []
                ctx["may_generate"] = (
                    ctx["book"].owner_id == request.user.pk
                    and bool(packet["items"])
                    and all(
                        s["report"].owner_id == request.user.pk
                        for s in ctx["comparisons"]
                    )
                )
                if (
                    request.method == "POST"
                    and request.POST.get("action") == "generate-rule"
                    and not notes
                ):
                    if not ctx["may_generate"]:
                        raise Http404
                    from core.glm import GLMClient

                    client = GLMClient()
                    if client.available:
                        try:
                            notes = reviews.generate_snapshot(
                                CombatLogMechanicReview, key, packet, client
                            )
                        except ValueError as exc:
                            ctx["ai_error"] = str(exc)
                    else:
                        ctx["ai_error"] = "尚未配置可用模型，可直接对照下表的真实轮次。"
                ctx["rule_notes"] = [
                    dict(n, urls=[refs[i] for i in n["lines"]]) for n in notes
                ]
        if panel != "review" and not params.get("rule"):
            return render(request, "dashboard/combat_explorer.html", ctx)
    return render(request, "dashboard/combat_workbench.html", ctx)


@endpoint("GET")
def workbench(request, pk):
    report = readable_report(request.user,pk)
    fight = analysis.nonnegative(request.GET, "fight")
    scope = source_context([report])
    if hasattr(report,"wcl_source"):
        from botend.dashboard.combat_wcl import search_context, source_directory, workspace_url
        origin = search_context(QueryDict(request.GET.get("origin","")))
        source = report.wcl_source
        scope["directory_url"] = scope["change_url"] = source_directory(report,origin)
        scope["wcl_progression"] = workspace_url([source["code"]],source["account"] is not None,origin,
                                                 group=source["focus"],discover=1 if origin else None)
    return render_analysis(
        request, [report], {**scope, "view": "single", "report": report}, selected=fight
    )


@endpoint("GET")
def player_comparison(request):
    return render_players(request, comparison_sources(request.user), {})


def compare_battle(request, reports, scope, pairing):
    params = request.GET
    samples, groups, _ = analysis.catalog(reports)
    target = params.get("target_group", "")
    run = params.get("run", "")
    if not target and params.get("battle"):
        battle = QueryDict(params["battle"])
        target = battle.get("cohort", "")
        source = battle.get("sample") or (str(reports[0].pk) if len(reports) == 1 else "")
        if source and battle.get("fight"):
            run = f"{source}.{battle['fight']}"
    if run:
        match = next((s for s in samples if f"{s['report'].pk}.{s['id']}" == run), None)
        if not match or target and match["cohort"] != target:
            raise Http404
        target = match["cohort"]
    if not target and len(groups) == 1:
        target = next(iter(groups))
    if target and target not in groups:
        raise Http404
    fields = [*scope.get("scope_fields", []), ("mode", "players")]
    for key in ("book", "battle", "back_catalog"):
        if params.get(key):
            fields.append((key, params[key]))
    base = "?" + urlencode(fields)
    overview, _ = analysis.overview(samples)
    targets = [(groups[row["cohort"]], row["count"],
                base + "&" + urlencode({"target_group": row["cohort"], "pairing": pairing})) for row in overview]
    picker = "?" + urlencode([(k, v) for k, v in fields if k != "battle"])
    ctx = dict(scope, comparing=True, targets=targets, target_group=target, pairing=pairing,
               target_label=groups.get(target), change_target=picker, form_fields=fields)
    template = "dashboard/combat_battle_comparison.html"
    if not target:
        return render(request, template, ctx)
    fields = [*fields, ("target_group", target)]
    choices = [(f"{s['report'].pk}.{s['id']}", s) for s in samples if s["cohort"] == target]
    run = run or choices[0][0]
    sample = dict(choices)[run]
    drill = QueryDict(params.get("battle", ""), mutable=True)
    if (drill.get("cohort") and drill["cohort"] != target
            or drill.get("sample") and drill["sample"] != str(sample["report"].pk)
            or drill.get("fight") and drill["fight"] != str(sample["id"])):
        drill = QueryDict(mutable=True)
    drill["cohort"] = target
    drill["sample"] = str(sample["report"].pk)
    drill["fight"] = str(sample["id"])
    if params.get("back_catalog"):
        drill["back_catalog"] = params["back_catalog"]
    drill.setlist("log", params.getlist("log") or [str(sample["report"].pk)])
    duel = player_filters(params)
    duel.update(target_group=target, run=run, pairing=pairing)
    drill["duel"] = urlencode(duel)
    ctx["battle_url"] = reverse("combat_workspace") + "?" + drill.urlencode()
    ctx.update(choices=choices, run=run, fight=sample, run_fields=fields + [("pairing", pairing)],
               form_fields=fields + [("pairing", pairing), ("run", run)])
    ctx["players_url"] = request.get_full_path()
    ctx["pairing_links"] = []
    for key, label in (("same", "同场角色"), ("across", "同目标跨场"), ("public", "公开标杆")):
        choice = {"pairing": key, "run": run}
        if params.get("left") and "across" not in (key, pairing):
            choice["left"] = params["left"]
        ctx["pairing_links"].append((key, label, "?" + urlencode([*fields, *choice.items()])))
    if pairing == "across":
        return ctx
    actors = combat_log_benchmark.participants(sample)
    metric = params.get("metric", "damage") if pairing == "same" else "damage"
    if metric not in combat_log_duel.DETAILS:
        raise ValueError("无效的对比指标。")
    breakdown = params.get("breakdown", "ability")
    dimensions = {"ability": "技能", "unit": "来源" if metric in ("taken", "deaths") else "目标", "time": "时段"}
    if breakdown not in dimensions:
        raise ValueError("无效的对比维度。")
    measures = {"total": "总量", "events": "事件次数", "average": "单次均值"}
    measure = "total" if metric in combat_log_duel.COUNT_METRICS else params.get("measure", "total")
    if measure not in measures:
        raise ValueError("无效的统计口径。")
    left, right = params.get("left", ""), params.get("right", "")
    ctx.update(actors=actors.values(), left=left, right=right, metrics=combat_log_duel.DETAILS, metric=metric,
               metric_label=combat_log_duel.DETAILS[metric], breakdown=breakdown, dimension_label=dimensions[breakdown])
    if left and left not in actors or pairing == "same" and right and right not in actors:
        raise ValueError("所选角色未参加这场战斗。")
    if left and pairing == "public":
        player = combat_log_benchmark.performance(sample, actors[left], metric)
        reference, reason = combat_log_benchmark.public_reference(sample, actors[left])
        ctx.update(local=player, reference=reference, reason=reason)
        if reference:
            ctx["rate_contrast"] = combat_log_duel.contrast(player["rate"], reference["dps"])
            ctx["duration_contrast"] = combat_log_duel.contrast(player["seconds"], reference["seconds"])
            detail = params.get("detail", "damage")
            scale = params.get("scale") or ("total" if player["seconds"] or detail in combat_log_duel.COUNT_METRICS else "share")
            scales = {"share": "技能占比", "total": "记录总量", "rate": "每分钟次数" if detail in combat_log_duel.COUNT_METRICS else "每秒数值"}
            if detail not in combat_log_duel.DETAILS or scale not in scales:
                raise ValueError("无效的公开对照指标或统计口径。")
            query = params.copy()
            query.pop("read_public", None)
            def public_link(field, value, anchor):
                fields = query.copy()
                fields[field] = value
                return "?" + fields.urlencode() + "#" + anchor
            data, warning = combat_public_events.snapshot(sample, reference, load=params.get("read_public") == "1")
            ctx.update(detail=detail, detail_label=combat_log_duel.DETAILS[detail], scale=scale,
                       read_url=public_link("read_public", "1", "public-data"), public_warning=warning,
                       rate_label=scales["rate"])
            if data:
                ctx.update(combat_log_benchmark.public_comparison(sample, actors[left], data, detail, scale))
                ctx["public_links"] = [(k, label, ctx["public_matrix"][k], public_link("detail", k, "comparison-detail"))
                                       for k, label in combat_log_duel.DETAILS.items()]
                ctx["scale_links"] = [(k, label, public_link("scale", k, "comparison-detail")) for k, label in scales.items()]
        if reference and player["rate"] is not None:
            ctx["gap"] = player["rate"] - reference["dps"]
    elif left and right:
        if left == right:
            ctx["reason"] = "请选择两名不同的参战角色。"
        else:
            ctx.update(combat_log_benchmark.pair_performance(sample, [actors[left], actors[right]], metric, breakdown, measure))
            def switch(field, value, anchor):
                q = params.copy()
                q[field] = value
                return "?" + q.urlencode() + "#" + anchor
            ctx["metric_links"] = [
                (k, label, ctx["matrix"][k]["delta"], switch("metric", k, "comparison-metrics"))
                for k, label in combat_log_duel.DETAILS.items()]
            ctx["dimension_links"] = [(k, label, switch("breakdown", k, "comparison-detail"))
                                      for k, label in dimensions.items()]
            if metric not in combat_log_duel.COUNT_METRICS:
                ctx["measure"] = measure
                ctx["measure_links"] = [(k, label, switch("measure", k, "comparison-detail"))
                                        for k, label in measures.items()]
            ctx["rate_label"] = {"damage": "DPS", "heal": "HPS"}.get(
                metric, "每分钟次数" if metric in combat_log_duel.COUNT_METRICS else "每秒" + ctx["metric_label"])
    return render(request, template, ctx)


def render_players(request, reports, scope):
    params = request.GET
    if scope:
        restore_directory(scope, params)
    if scope and params.get("book"):
        book = get_object_or_404(collections(request.user), pk=params["book"])
        scope["battle_url"] = reverse("combat_collection", args=[book.pk])
    if scope and params.get("battle"):
        state = QueryDict(params["battle"])
        allowed = set(FILTERS) | {"fight", "wave", "part", "sample", "series", "attempt"}
        restored = [(k, v) for k, values in state.lists() if k in allowed for v in values]
        if restored:
            separator = "&" if "?" in scope["battle_url"] else "?"
            scope["battle_url"] += separator + urlencode(restored)
    options = params.get("options")
    pairing = params.get("pairing")
    if pairing and pairing not in ("same", "across", "public"):
        raise ValueError("未知的对比方式。")
    # 既有的双角色深链接继续按共同活动匹配，新入口先选目标和场次。
    guided = pairing or params.get("run") or params.get("target_group") or not (options or params.get("left") or params.get("right"))
    setup = {}
    if guided:
        pairing = pairing or "same"
        setup = compare_battle(request, list(reports), scope, pairing)
        if not isinstance(setup, dict):
            return setup
    if options and options not in ("left", "right"):
        raise Http404
    if options and params.get(options + "_file"):
        requested = params[options + "_file"]
        if scope:
            reports = [report for report in reports if str(report.pk) == requested]
        else:
            reports = reports.filter(pk=requested)
    sources, groups = combat_log_duel.directory(reports, setup.get("target_group", ""))
    if scope and not options:
        filters = player_filters(params)
        if filters:
            duel = urlencode({"duel": urlencode(filters)})
            if not setup:
                separator = "&" if "?" in scope["battle_url"] else "?"
                scope["battle_url"] += separator + duel
            base = reverse("combat_workspace") + "?" + urlencode(scope["scope_fields"])
            for source in sources:
                for sample in source["choices"]:
                    selection = urlencode({"sample": source["report"].pk, "fight": sample["id"]})
                    sample["url"] = base + "&" + selection + "&" + duel
    metric = params.get("metric", "damage")
    if metric not in combat_log_duel.METRICS:
        raise ValueError("角色最佳场次支持每秒伤害或每秒有效治疗。")
    cohort = params.get("cohort", "")
    ctx = dict(
        scope | setup,
        comparing=True,
        groups=groups,
        sources=sources,
        metrics=combat_log_duel.METRICS,
        metric=metric,
        cohort=cohort,
    )
    sides = []
    for i, (key, label) in enumerate((("left", "角色 A"), ("right", "角色 B"))):
        if options and key != options:
            continue
        default_file = ""
        if len(scope.get("scope_logs", ())) == 2 and len(sources) == 2 and not options:
            default_file = str(sources[i]["report"].pk)
        file = params.get(key + "_file", default_file)
        scoped = [s for s in sources if not file or str(s["report"].pk) == file]
        if file and not scoped:
            raise Http404
        guid = params.get(key, "")
        fight = params.get(key + "_fight", "") if file else ""
        actors = {}
        for source in scoped:
            actors.update(source["actors"])
        side = dict(
            key=key,
            label=label,
            file=file,
            guid=guid,
            fight=fight,
            name=actors.get(guid),
            actors=sorted(actors.items(), key=lambda it: (it[1], it[0])),
            fights=scoped[0]["choices"] if file else [],
        )
        sides.append(side)
    if options:
        return render(request, "dashboard/combat_duel_roster.html", {"side": sides[0]})
    ctx["sides"] = sides
    if all(s["guid"] for s in sides):
        if any(not s["name"] for s in sides):
            ctx["compare_error"] = "角色不在当前所选日志中，请重新选择。"
        elif sides[0]["guid"] == sides[1]["guid"] and not (
            sides[0]["file"]
            and sides[1]["file"]
            and (sides[0]["file"], sides[0]["fight"])
            != (sides[1]["file"], sides[1]["fight"])
        ):
            ctx["compare_error"] = (
                "请选择两名角色。同一角色也可指定两份不同日志或不同场次。"
            )
        else:
            detail = params.get("detail") or metric
            if detail not in combat_log_duel.DETAILS:
                raise ValueError("未知的角色对照指标。")
            ctx.update(
                combat_log_duel.comparison(
                    sources, groups, sides, metric, cohort, detail
                )
            )
            if ctx.get("winners"):
                pinned = params.copy()
                for side, winner in zip(sides, ctx["winners"]):
                    sample = winner["sample"]
                    pinned[side["key"] + "_file"] = str(sample["report"].pk)
                    pinned[side["key"] + "_fight"] = str(sample["id"])
                swapped = params.copy()
                for suffix in ("", "_file", "_fight"):
                    left, right = "left" + suffix, "right" + suffix
                    field = suffix.lstrip("_") or "guid"
                    swapped[left], swapped[right] = sides[1][field], sides[0][field]
                ctx["swap_url"] = "?" + swapped.urlencode() + "#comparison-result"
                ctx["pin_url"] = "?" + pinned.urlencode() + "#comparison-result"
            tabs = []
            for key, label in combat_log_duel.DETAILS.items():
                q = params.copy()
                q["detail"] = key
                tabs.append(
                    (label, "?" + q.urlencode() + "#duel-breakdown", key == detail)
                )
            ctx.update(
                detail_tabs=tabs,
                detail=detail,
                unit="分钟" if detail in combat_log_duel.COUNT_METRICS else "秒",
            )
    return render(request, "dashboard/combat_player_comparison.html", ctx)


@endpoint("GET")
def spell_icon_batch(request, pk):
    ids = list(dict.fromkeys(request.GET.getlist("id")))
    if not 1 <= len(ids) <= 8 or any(not 0 < visuals.localization.number(s) <= 2147483647 for s in ids):
        raise ValueError("每次可查询 1 至 8 个有效技能编号。")
    report = readable_report(request.user, pk)
    if report.summary.get("index_version", 0) < analysis.VERSION:
        raise Http404
    with analysis.database(report) as db:
        return JsonResponse({"icons": combat_replay_activity.lookup_report_icons(db, ids)})


@endpoint("GET")
def replay_map(request, build, art):
    try:
        content = combat_replay_maps.native_art(build, art)
    except LookupError:
        raise Http404
    except (OSError, RequestException, ValueError):
        return HttpResponse("楼层图暂时无法读取。", status=503)
    return HttpResponse(content, content_type="image/webp")


@endpoint("GET")
def replay(request, pk, fight):
    from botend.services.combat_loading import message
    message("正在读取回放时间范围、单位和地图信息。")
    report = readable_report(request.user,pk)
    if report.summary.get("index_version", 0) < 3:
        raise Http404
    if "cast_icon" in request.GET:
        sid = analysis.nonnegative(request.GET, "cast_icon")
        if not sid or sid > 2147483647:
            raise Http404
        if request.GET.get("revision",str(report.published)) != str(report.published):
            return HttpResponse(status=409)
        with analysis.database(report) as db:
            battle = db.execute("SELECT * FROM fight WHERE id=?", (fight,)).fetchone()
            icon = combat_replay_activity.missing_cast_icon(
                db,battle,sid,combat_replay_activity.boss_groups(db,report,battle)
            ) if battle else ""
        if not icon:
            raise Http404
        return redirect(icon)
    chunk = analysis.nonnegative(request.GET, "chunk")
    subject = analysis.nonnegative(request.GET, "subject")
    threshold = analysis.nonnegative(request.GET, "threshold")
    threshold_page = analysis.nonnegative(request.GET, "threshold_page", 0)
    if threshold is not None and (not 1 <= threshold <= 100 or subject is None or chunk is not None or "event_page" in request.GET):
        raise ValueError("请选择单位和 1 至 100 的生命百分比。")
    if subject is not None and chunk is None and threshold is None:
        raise ValueError("请选择回放时段。")
    event_page = analysis.nonnegative(request.GET, "event_page")
    casts_only = request.GET.get("track") == "casts"
    if casts_only and (chunk is None or subject is not None or event_page is not None):
        raise ValueError("请选择施法时间段。")
    query = request.GET.get("q", "").strip()
    if len(query) > 100 or (chunk is not None and event_page is not None):
        raise ValueError("回放查询参数无效。")
    if (chunk is not None or event_page is not None or threshold is not None) and request.GET.get("revision", str(report.published)) != str(report.published):
        return JsonResponse({"message": "索引已更新，请刷新回放。"}, status=409)
    try:
        battle, data = combat_log_replay.read(
            report, fight, chunk, event_page=event_page, query=query, kind=request.GET.get("kind", ""), subject=subject,
            threshold=threshold, threshold_page=threshold_page, party_auras=request.GET.get("party_auras") == "1",
            casts_only=casts_only,
        )
    except LookupError:
        raise Http404
    if chunk is not None or event_page is not None or threshold is not None:
        return JsonResponse(data)
    analysis_url = analysis.work_url(report, fight)
    back = request.GET.get("back", "")
    scope = source_context([report])
    wave_fields = []
    if back:
        previous = QueryDict(back, mutable=True)
        previous["fight"] = str(fight)
        if previous.getlist("log"):
            reports = selected_reports(request.user, previous)
            if report.pk not in {r.pk for r in reports}:
                raise Http404
            scope = source_context(reports)
            previous["sample"] = str(report.pk)
            wave_fields = [*scope["scope_fields"], ("sample", str(report.pk))]
            analysis_url = reverse("combat_workspace") + "?" + previous.urlencode()
        else:
            analysis_url = reverse("combat_analysis", args=[report.pk]) + "?" + previous.urlencode()
    state = QueryDict(analysis_url.partition("?")[2], mutable=True)
    restore_directory(scope, state)
    scope["battle_url"] = analysis_url
    link_players(scope, state)
    return render(request, "dashboard/combat_replay.html", {
        **scope, "replay_wave_fields": wave_fields,
        "report": report, "fight": battle, "replay": data,
        "legacy_format20": report.summary.get("position_revision", 0) < 3 and any(
            session.get("format") == "20" and session.get("advanced")
            for session in report.summary.get("sessions", [])
        ),
        "analysis_url": analysis_url,
    })


@endpoint("GET")
def actor_view(request, pk, fight, guid):
    report = readable_report(request.user,pk)
    if report.summary.get("index_version", 0) < analysis.VERSION:
        raise Http404
    try:
        context = combat_log_actor.inspect(
            report, fight, guid, analysis.nonnegative(request.GET, "after", 0),
            analysis.nonnegative(request.GET, "from_ms", 0), analysis.nonnegative(request.GET, "to_ms"),
        )
    except LookupError:
        raise Http404
    back = QueryDict(request.GET.get("back", ""), mutable=True)
    back["fight"] = str(fight)
    route = reverse("combat_analysis", args=[pk])
    if back.getlist("log"):
        if report.pk not in {r.pk for r in selected_reports(request.user, back)}:
            raise Http404
        back["sample"] = str(report.pk)
        route = reverse("combat_workspace")
    context["analysis_url"] = route + "?" + back.urlencode()
    if context["cursor"]:
        query = request.GET.copy()
        query["after"] = context["cursor"]
        context["next_casts"] = "?" + query.urlencode()
    restore_directory(context, back)
    if hasattr(report, "wcl_source") and not context.get("directory_url"):
        from botend.dashboard.combat_wcl import source_directory
        context["directory_url"] = source_directory(report)
    return render(
        request,
        "dashboard/combat_workbench.html",
        dict(context, view="actor", report=report, fight_id=fight),
    )


@endpoint("GET")
def event(request, pk, line):
    report = readable_report(request.user,pk)
    if report.summary.get("index_version", 0) < 2:
        raise Http404
    with analysis.database(report) as db:
        row = db.execute(
            """SELECT f.*,s.name source,t.name target,a.name ability FROM fact f
            JOIN actor s ON s.id=f.src JOIN actor t ON t.id=f.dst JOIN ability a ON a.id=f.spell
            WHERE f.line=?""",
            (line,),
        ).fetchone()
        if row and hasattr(report,"wcl_source"):
            payload = db.execute("SELECT payload FROM wcl_event WHERE line=?",(line,)).fetchone()
            raw_event = zlib.decompress(payload[0])
        else:
            raw_event = None
    if not row:
        raise Http404
    row = dict(row)
    row["kind_label"] = analysis.event_label(row["kind"])
    start, remaining = row["offset"], min(row["length"], 65536)
    raw = bytearray()
    if raw_event is not None:
        raw.extend(raw_event[:remaining])
        remaining = 0
    while remaining:
        n, within = divmod(start, report.part_size)
        chunk = archive.read_block(report, report.parts.get(number=n).digest)
        piece = chunk[within : within + remaining]
        if not piece:
            raise ValueError("归档块与事件索引不一致。")
        raw.extend(piece)
        remaining -= len(piece)
        start += len(piece)
    if request.headers.get("X-Combat-View") == "evidence":
        return JsonResponse({
            "line": line, "filename": report.filename, "source": row["source"], "target": row["target"],
            "ability": row["ability"], "kind": row["kind"], "kind_label": row["kind_label"], "amount": row["amount"],
            "raw": raw.decode("utf-8", "replace"), "clipped": len(raw) < row["length"],
        })
    return render(
        request,
        "dashboard/combat_workbench.html",
        {
            "view": "event",
            "report": report,
            "event": row,
            "raw": raw.decode("utf-8", "replace"),
            "clipped": len(raw) < row["length"],
            "fight_id": row["boss"] or row["keystone"],
            "analysis_url": analysis.work_url(report, row["boss"] or row["keystone"]) if row["boss"] or row["keystone"] else reverse("combat_report", args=[report.pk]),
        },
    )


@endpoint("GET", "POST")
def review(request, pk, fight, topic, anchor):
    report = readable_report(request.user,pk)
    if report.summary.get("index_version", 0) < 2:
        raise Http404
    try:
        packet = reviews.evidence(report, fight, topic, anchor)
    except LookupError:
        raise Http404
    key = dict(
        report=report, version=report.published, fight=fight, topic=topic, anchor=anchor
    )
    saved = None if hasattr(report,"wcl_source") else CombatLogReview.objects.filter(**key).first()
    findings = saved.findings if saved else []
    error = ""
    if request.method == "POST":
        if report.owner_id != request.user.pk:
            raise Http404
        if not findings:
            from core.glm import GLMClient

            client = GLMClient()
            if not client.available:
                error = "尚未配置可用模型。"
            else:
                try:
                    findings = reviews.generate(
                        report, fight, topic, anchor, packet, client
                    )
                except ValueError as exc:
                    error = str(exc)
    return render(
        request,
        "dashboard/combat_workbench.html",
        {
            "view": "review",
            "report": report,
            "packet": packet,
            "analysis_url": analysis.work_url(report, fight) + ("&metric=deaths&panel=events" if topic == "death" else ""),
            "findings": findings,
            "error": error,
            "fight_id": fight,
        },
    )
