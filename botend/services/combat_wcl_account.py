"""WCL 用户授权、加密凭据和按报告范围读取的查询。"""

import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from Crypto.Cipher import AES
from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import WclUserBinding
from botend.services.wcl_api_budget import graphql_request

AUTH = "https://www.warcraftlogs.com/oauth/authorize"
TOKEN = "https://www.warcraftlogs.com/oauth/token"
SESSION_KEY = "combat_wcl_oauth"
IDENTITY = "{userData{currentUser{id name}}}"
FIGHT_FIELDS = """id encounterID name difficulty kill startTime endTime
  fightPercentage bossPercentage lastPhase keystoneLevel keystoneAffixes"""
REPORT_FIELDS = "code title startTime zone{id name} owner{name}"
CATALOG_FIELDS = REPORT_FIELDS + " fights(killType:Encounters){" + FIGHT_FIELDS + "}"
REPORTS = """query($user:Int!,$page:Int!){reportData{reports(userID:$user,page:$page,limit:20){
  data{FIELDS} has_more_pages}}}""".replace("FIELDS", CATALOG_FIELDS)
CHARACTER_REPORTS = """query($name:String!,$realm:String!,$region:String!,$page:Int!){
  characterData{character(name:$name,serverSlug:$realm,serverRegion:$region){
  recentReports(limit:20,page:$page){data{FIELDS} has_more_pages}}}}""".replace("FIELDS", CATALOG_FIELDS)
GUILD_REPORTS = """query($name:String!,$realm:String!,$region:String!,$page:Int!){
  reportData{reports(guildName:$name,guildServerSlug:$realm,guildServerRegion:$region,limit:20,page:$page){
  data{FIELDS} has_more_pages}}}""".replace("FIELDS", CATALOG_FIELDS)
FIGHT_DETAIL = """query($code:String!,$ids:[Int]!,$start:Float!,$end:Float!){
  reportData{report(code:$code){
    damage:table(dataType:DamageDone,fightIDs:$ids,startTime:$start,endTime:$end)
    healing:table(dataType:Healing,fightIDs:$ids,startTime:$start,endTime:$end)
    masterData{actors{id name server} abilities{gameID name}}
    deaths:events(dataType:Deaths,fightIDs:$ids,startTime:$start,endTime:$end,limit:10000){data nextPageTimestamp}
  }}}"""


class WclError(Exception):
    status = 503


class AccessDenied(WclError):
    status = 403


class ReportUnavailable(WclError):
    status = 404


class GrantExpired(AccessDenied):
    """本站登录有效，但 WCL 用户凭据需要重新授权。"""


def config():
    return getattr(settings, "WCL_V2_CONFIG", {}) or {}


def configured():
    return all(config().get(key) for key in ("client_id", "client_secret"))


def proxies():
    cfg = getattr(settings, "REQUEST_CONFIG", {})
    return cfg.get("proxies") if cfg.get("enable_proxy") else None


def callback_uri(request):
    return config().get("redirect_uri") or request.build_absolute_uri(reverse("combat_wcl_callback"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def authorize(request):
    if not configured():
        raise WclError("站点尚未配置 WCL 应用凭据。")
    state = secrets.token_urlsafe(32)
    redirect_uri = callback_uri(request)
    WclUserBinding.objects.update_or_create(user=request.user, defaults={"challenge": digest(state)})
    request.session[SESSION_KEY] = {
        "state": state, "uid": request.user.pk, "issued": timezone.now().timestamp(), "uri": redirect_uri,
    }
    return AUTH + "?" + urlencode(dict(client_id=config()["client_id"], response_type="code",
                                       scope="view-user-profile view-private-reports",
                                       redirect_uri=redirect_uri, state=state))


def exchange(fields):
    if not configured():
        raise WclError("站点尚未配置 WCL 应用凭据。")
    try:
        r = requests.post(TOKEN, auth=(config()["client_id"], config()["client_secret"]),
                          data=fields, timeout=15, proxies=proxies(), allow_redirects=False)
        body = r.json()
    except (requests.RequestException, ValueError):
        raise WclError("暂时无法连接 WCL 授权服务。") from None
    if not isinstance(body, dict):
        raise WclError("WCL 返回了无法识别的授权信息。")
    if body.get("error") == "invalid_grant":
        raise GrantExpired("WCL 授权已失效，请重新授权。")
    if r.status_code != 200 or not isinstance(body.get("access_token"), str) or not body["access_token"]:
        raise WclError("WCL 未签发令牌，请核对客户端类型、凭据和注册的回调地址。")
    try:
        ttl = int(body["expires_in"])
        if ttl <= 0:
            raise ValueError
        expires = timezone.now() + timedelta(seconds=ttl)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise WclError("WCL 返回的令牌有效期无效。") from None
    return {"access": body["access_token"], "refresh": body.get("refresh_token") or ""}, expires


def cipher_key(uid):
    return salted_hmac("LMonitor.WCL.user.v1", str(uid), algorithm="sha256").digest()


def seal(uid, tokens):
    cipher = AES.new(cipher_key(uid), AES.MODE_GCM)
    body, tag = cipher.encrypt_and_digest(json.dumps(tokens).encode())
    return base64.b64encode(cipher.nonce + tag + body).decode()


def unseal(binding):
    try:
        raw = base64.b64decode(binding.credentials, validate=True)
        cipher = AES.new(cipher_key(binding.user_id), AES.MODE_GCM, nonce=raw[:16])
        return json.loads(cipher.decrypt_and_verify(raw[32:], raw[16:32]))
    except (ValueError, KeyError):
        raise GrantExpired("请重新授权 WCL 账号。") from None


def query_user(token, query, variables):
    result = graphql_request(config().get("client_id"), token, query, variables,
                             user_authorized=True, proxies=proxies())
    if result.error == "authentication":
        raise GrantExpired("WCL 授权已失效，请重新授权。")
    if result.error in ("permission", "http_403"):
        raise AccessDenied("当前 WCL 账号没有这份报告的访问权限。")
    if result.error:
        raise WclError("WCL 查询暂时不可用，请稍后重试。")
    return result.payload["data"]


def complete(request):
    pending = request.session.get(SESSION_KEY) or {}
    state = request.GET.get("state", "")
    if (not state or not state.isascii() or len(state) > 256 or pending.get("uid") != request.user.pk
            or not secrets.compare_digest(pending.get("state", ""), state)
            or not 0 <= timezone.now().timestamp() - pending.get("issued", 0) <= 600):
        raise WclError("绑定请求已过期或与当前会话不匹配，请重新发起。")
    request.session.pop(SESSION_KEY, None)
    # 一次性领取回调，新授权或解除绑定会使进行中的旧请求失效。
    claim = secrets.token_hex(32)
    binding = WclUserBinding.objects.filter(user=request.user)
    if not binding.filter(challenge=digest(state)).update(challenge=claim):
        raise WclError("这次授权请求已处理，请重新发起。")
    if request.GET.get("error") or not request.GET.get("code"):
        raise WclError("本次 WCL 授权未完成，原有绑定保持有效。" if binding.filter(wcl_id__isnull=False).exists()
                       else "本次 WCL 授权未完成。")
    tokens, expires = exchange(dict(grant_type="authorization_code", code=request.GET["code"], redirect_uri=pending["uri"]))
    identity = (query_user(tokens["access"], IDENTITY, {}).get("userData") or {}).get("currentUser")
    if not identity or not identity.get("id") or not identity.get("name"):
        raise WclError("无法确认授权账号身份，请重新授权。")
    if not binding.filter(challenge=claim).update(wcl_id=identity["id"], name=identity["name"], challenge="",
            credentials=seal(request.user.pk, tokens), expires_at=expires, refreshing_until=None):
        raise WclError("绑定状态已改变，请重新打开账号页面。")


def access_token(binding, force=False):
    tokens = unseal(binding)
    now = timezone.now()
    if not force and binding.expires_at and binding.expires_at > now + timedelta(seconds=30):
        return tokens["access"]
    if not tokens.get("refresh"):
        raise GrantExpired("WCL 授权已过期，请重新授权。")
    current = WclUserBinding.objects.filter(pk=binding.pk, credentials=binding.credentials)
    lease = now + timedelta(minutes=1)
    if not current.filter(Q(refreshing_until__isnull=True) | Q(refreshing_until__lte=now)).update(refreshing_until=lease):
        raise WclError("授权状态正在更新，请刷新后再试。")
    locked = current.filter(refreshing_until=lease)
    try:
        fresh, expires = exchange(dict(grant_type="refresh_token", refresh_token=tokens["refresh"]))
        fresh["refresh"] = fresh["refresh"] or tokens["refresh"]
        encrypted = seal(binding.user_id, fresh)
        if not locked.update(credentials=encrypted, expires_at=expires, refreshing_until=None):
            raise WclError("绑定状态已改变，请刷新后再试。")
        binding.credentials, binding.expires_at = encrypted, expires
        return fresh["access"]
    except GrantExpired:
        locked.update(credentials="", expires_at=None, refreshing_until=None)
        raise
    finally:
        locked.update(refreshing_until=None)


def read(binding, query, variables):
    if not binding:
        client = SpecDetailBase(None, None)
        result = client._wcl_graphql(query, variables)
        if result is None:
            raise WclError("WCL 未返回公开数据，请核对查询条件或稍后再试。")
        return result
    token = access_token(binding)
    try:
        return query_user(token, query, variables)
    except GrantExpired:
        return query_user(access_token(binding, force=True), query, variables)
