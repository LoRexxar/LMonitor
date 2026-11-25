"""后台按网站账号管理 WCL 公开角色名额。"""
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from botend.dashboard.combat_logs import endpoint
from botend.dashboard.permissions import dashboard_permission_required
from botend.models import CombatPublicRolePolicy, CombatPublicSource

SECTION = "combat-public-roles"


def filters(query):
    return {k: v for k, v in (("section", SECTION), ("q", query.get("q", "").strip())) if v}


def context(query):
    params = filters(query)
    roles = CombatPublicSource.objects.filter(kind="character").order_by("name", "region", "realm")
    accounts = get_user_model().objects.order_by("username", "pk").prefetch_related(
        Prefetch("combatpublicsource_set", queryset=roles, to_attr="public_roles"))
    if "q" in params:
        accounts = accounts.filter(username__icontains=params["q"])
    page = Paginator(accounts, 25).get_page(query.get("page"))
    policies = CombatPublicRolePolicy.objects.in_bulk(["default", *(str(u.pk) for u in page)])
    default = policies.get("default") or CombatPublicRolePolicy()
    rows = [dict(scope="default", label="全站默认", limit=default.limit)]
    for u in page:
        key = str(u.pk)
        rows.append(dict(scope=key, label=u.username, custom=key in policies,
                         sources=u.public_roles, limit=policies.get(key, default).limit))
    return dict(role_rows=rows, role_page=page, role_filters=urlencode(params), role_query=params.get("q", ""),
                role_saved=query.get("saved"))


@endpoint("POST")
@dashboard_permission_required("system.combat-public-roles")
def save(request):
    key = request.POST.get("scope")
    if key != "default":
        if not key or not key.isascii() or not key.isdecimal() or len(key) > 19 or int(key) > 2**63-1:
            raise ValueError("请选择有效的网站账号。")
        key = str(get_object_or_404(get_user_model(), pk=int(key)).pk)
    if request.POST.get("inherit"):
        if key == "default":
            raise ValueError("全站默认名额不能继承。")
        CombatPublicRolePolicy.objects.filter(scope=key).delete()
    else:
        value = request.POST.get("limit", "")
        if not value.isascii() or not value.isdecimal() or len(value)>4 or int(value)>1000:
            raise ValueError("角色上限须为 0 到 1000 之间的整数。")
        CombatPublicRolePolicy.objects.update_or_create(scope=key, defaults={"limit": int(value)})
    params = dict(filters(request.GET), saved=key, page=request.GET.get("page", "1"))
    return redirect(reverse("dashboard")+"?"+urlencode(params)+"#role-account-"+key)
