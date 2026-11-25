"""公开角色关联、专精展示和独立的账号名额。"""

from types import SimpleNamespace

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import F, Q

from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON, canonical_class_spec
from botend.models import CombatPublicRolePolicy, CombatPublicSource, UserCharacterBinding
from botend.raiderio_identity import normalized_name
from botend.services.player_raiderio_activity import player_identity


def limit_for(user):
    policies = CombatPublicRolePolicy.objects.in_bulk(["default", str(user.pk)])
    return (policies.get(str(user.pk)) or policies.get("default") or CombatPublicRolePolicy()).limit


def decorate(row, scope):
    row.spec_icon = SPEC_ICON.get(scope, "")
    row.spec_label = (f"{SPEC_CN.get(scope[1], scope[1])} · {CLASS_CN.get(scope[0], scope[0])}"
                      if scope else "暂无专精资料")
    return row


def bound_roles(user):
    return UserCharacterBinding.objects.filter(user=user).select_related("player")


def profile(name, region, realm):
    """服务器别名由带区服和服务器的角色资料请求解析。"""
    cfg = getattr(settings, "REQUEST_CONFIG", {})
    try:
        response = requests.get("https://raider.io/api/v1/characters/profile",
                                params={"region": region, "realm": realm, "name": name},
                                timeout=5, proxies=cfg.get("proxies") if cfg.get("enable_proxy") else None)
        response.raise_for_status()
        data = response.json()
        identity = player_identity(SimpleNamespace(region=data["region"], realm=data["realm"],
                                   character_name=data["name"], profile_url=data["profile_url"]))
        if (not identity or identity["region"] != region.lower()
                or identity["character_name"] != normalized_name(name)):
            return None, ""
        return canonical_class_spec(data.get("class"), data.get("active_spec_name")), identity["realm_slug"]
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        return None, ""


def bound_scope(binding):
    return canonical_class_spec(binding.player.class_name, binding.player.spec_name) if binding.player_id else None


def add(user, identity, scope=None, profile_realm=""):
    with transaction.atomic():
        accounts = get_user_model().objects
        if connection.vendor == "sqlite":
            accounts.filter(pk=user.pk).update(last_login=F("last_login"))
        else:
            accounts.select_for_update().get(pk=user.pk)
        roles = CombatPublicSource.objects.filter(user=user, kind=identity["kind"])
        realms = Q(realm__iexact=identity["realm"]) | Q(profile_realm__iexact=identity["realm"])
        if profile_realm:
            realms |= Q(realm__iexact=profile_realm) | Q(profile_realm__iexact=profile_realm)
        source = roles.filter(realms, name__iexact=identity["name"], region__iexact=identity["region"]).first()
        if source is None:
            if identity["kind"] == "character" and roles.count() >= limit_for(user):
                raise ValueError("公开角色名额已满，请先取消一个关联。")
            source = CombatPublicSource(user=user, **identity)
        if scope:
            source.class_name, source.spec_name = scope
        if profile_realm:
            source.profile_realm = profile_realm
        source.save()
        return source


def roster(user):
    sources = list(CombatPublicSource.objects.filter(user=user).order_by("kind", "name", "realm"))
    bindings = list(bound_roles(user))
    owned = {(b.region.lower(), b.realm_slug.casefold(), normalized_name(b.character_name)): b for b in bindings}
    for s in sources:
        if s.kind != "character":
            continue
        name = normalized_name(s.name)
        region = s.region.lower()
        binding = owned.get((region, s.realm.casefold(), name)) or owned.get((region, s.profile_realm.casefold(), name))
        scope = bound_scope(binding) if binding else None
        if binding:
            binding.linked = True
        decorate(s, scope or canonical_class_spec(s.class_name, s.spec_name))
    for b in bindings:
        decorate(b, bound_scope(b))
    count = sum(s.kind == "character" for s in sources)
    limit = limit_for(user)
    return dict(sources=sources, bound_roles=bindings, role_count=count, role_limit=limit, at_limit=count >= limit)


def refresh_profile(source):
    key = f"combat-public-profile:{source.pk}"
    if not cache.add(key, True, 300):
        if source.spec_name:
            return decorate(source, canonical_class_spec(source.class_name, source.spec_name))
        raise ValueError("专精资料暂不可用，请稍后再试。")
    scope, realm = profile(source.name, source.region, source.realm)
    if not scope:
        raise ValueError("暂未取得专精资料，仍可查看日志。")
    CombatPublicSource.objects.filter(pk=source.pk).update(class_name=scope[0], spec_name=scope[1], profile_realm=realm)
    return decorate(source, scope)
