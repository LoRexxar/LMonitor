import re
import time
import datetime
import json

from botend.controller.BaseScan import BaseScan
from botend.models import PortalVideo, VideoMonitorTarget
from django.utils import timezone
from utils.log import logger
from botend.models import TargetAuth
from botend.alerting import upsert_system_alert
from urllib.parse import urlparse


BILIBILI_DYNAMIC_FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote"


def _extract_mid(url):
    u = (url or '').strip()
    m = re.search(r"space\.bilibili\.com/(\d+)", u)
    if not m:
        return None
    return m.group(1)


def _normalize_bilibili_url(url):
    value = (url or '').strip()
    if value.startswith('//'):
        return f"https:{value}"
    if value.startswith('/'):
        return f"https://www.bilibili.com{value}"
    return value


class PortalVideoMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        targets = VideoMonitorTarget.objects.filter(is_active=True).all()
        for t in targets:
            try:
                self.update_target(t)
                time.sleep(1)
            except Exception as e:
                logger.error(f"[PortalVideoMonitor] target error: {str(e)}")
                try:
                    upsert_system_alert(
                        category='VIDEO_MONITOR_TARGET_ERROR',
                        subject=str(getattr(t, 'target_url', '') or '')[:128],
                        level=2,
                        title='视频监控任务异常',
                        content=str(e)
                    )
                except Exception:
                    pass
        return True

    def update_target(self, target):
        mid = _extract_mid(target.target_url)
        if not mid:
            return

        dynamic_api = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={mid}&offset=&features={BILIBILI_DYNAMIC_FEATURES}"
        arc_api = f"https://api.bilibili.com/x/space/arc/search?mid={mid}&pn=1&ps=20&order=pubdate"
        domain = urlparse(dynamic_api).netloc
        cookies = self._get_bilibili_cookies(domain)

        payload, fetch_error = self._fetch_bilibili_payload(dynamic_api, cookies, target.target_url, "dynamic")
        if self._payload_has_error(payload, fetch_error, domain, dynamic_api, "dynamic"):
            return
        videos = self._parse_dynamic_videos(payload)

        if not videos:
            payload, fetch_error = self._fetch_bilibili_payload(arc_api, cookies, target.target_url, "arc")
            if self._payload_has_error(payload, fetch_error, domain, arc_api, "arc"):
                return
            videos = self._parse_arc_videos(payload)

        if not videos:
            return

        newest_bvid = None
        for item in videos:
            bvid = (item.get("bvid") or "").strip()
            if not bvid:
                continue
            if not newest_bvid:
                newest_bvid = bvid
            published_at = self._parse_published_at(item.get("published_ts"))
            video_url = _normalize_bilibili_url(item.get("url")) or f"https://www.bilibili.com/video/{bvid}"
            PortalVideo.objects.update_or_create(
                url=video_url,
                defaults={
                    "title": (item.get("title") or bvid).strip(),
                    "bvid": bvid,
                    "cover_url": (item.get("cover_url") or "").strip() or None,
                    "published_at": published_at,
                    "author_name": (item.get("author_name") or target.name or "").strip(),
                    "author_url": target.target_url or f"https://space.bilibili.com/{mid}",
                    "tag": target.tag,
                    "target": target,
                    "is_active": True,
                },
            )

        if newest_bvid and newest_bvid != target.last_seen_bvid:
            target.last_seen_bvid = newest_bvid
            target.save(update_fields=["last_seen_bvid"])

    def _get_bilibili_cookies(self, domain):
        cookies = ""
        try:
            auth = TargetAuth.objects.filter(domain=domain).first()
            if auth and auth.cookie:
                cookies = auth.cookie
            if not cookies:
                auth2 = TargetAuth.objects.filter(domain="bilibili.com").first()
                if auth2 and auth2.cookie:
                    cookies = auth2.cookie
        except Exception:
            cookies = ""
        return cookies

    def _fetch_bilibili_payload(self, api, cookies, referer, source):
        payload = None
        fetch_error = ""
        try:
            if self.req:
                headers = {"Referer": referer, "Accept": "application/json"}
                resp = self.req.get(api, 'Response', 0, cookies, headers)
                if not resp:
                    fetch_error = f"{source} 接口请求失败"
                elif int(getattr(resp, "status_code", 0) or 0) != 200:
                    code = int(getattr(resp, "status_code", 0) or 0)
                    fetch_error = f"{source} HTTP {code}"
                else:
                    raw = getattr(resp, "text", "") or ""
                    if raw.strip():
                        payload = json.loads(raw)
                    else:
                        fetch_error = f"{source} 接口返回为空"
            else:
                import requests
                headers = {"User-Agent": "Mozilla/5.0", "Referer": referer, "Cookie": cookies}
                resp = requests.get(api, timeout=20, headers=headers)
                if resp.status_code != 200:
                    fetch_error = f"{source} HTTP {resp.status_code}"
                else:
                    payload = resp.json() or {}
        except Exception as e:
            fetch_error = f"{source} {str(e)}"
            payload = None
        return payload, fetch_error

    def _payload_has_error(self, payload, fetch_error, domain, api, source, alert=True):
        if not isinstance(payload, dict) or not payload:
            if fetch_error and alert:
                try:
                    if 'HTTP 412' in fetch_error:
                        upsert_system_alert(
                            category='BILIBILI_COOKIE_REQUIRED',
                            subject=domain,
                            level=3,
                            title=f'B站{source}接口被风控(412)',
                            content=f"接口返回 {fetch_error}，通常需要配置 cookie 才能稳定访问。\n请更新 TargetAuth(domain={domain}) 或 TargetAuth(domain=bilibili.com) 的 cookie。\n{api}"
                        )
                        return True
                    upsert_system_alert(
                        category='BILIBILI_API_FAILED',
                        subject=domain,
                        level=2,
                        title=f'B站视频{source}接口请求失败',
                        content=f"{fetch_error}\n{api}"
                    )
                except Exception:
                    pass
            return True

        code = payload.get('code')
        msg = payload.get('message') or payload.get('msg') or ''
        if code is not None:
            try:
                code_i = int(code)
            except Exception:
                code_i = None
            if code_i is not None and code_i != 0:
                if not alert:
                    return True
                is_rate_limited = code_i == -799 or ('频繁' in str(msg)) or ('稍后再试' in str(msg))
                if is_rate_limited:
                    upsert_system_alert(
                        category='BILIBILI_RATE_LIMIT',
                        subject=domain,
                        level=2,
                        title=f'B站{source}接口请求过于频繁',
                        content=f"B站{source}接口返回 code={code_i} {msg}，请降低视频监控频率或配置 TargetAuth(domain={domain}) 的 cookie。"
                    )
                    return True

                should_alert = code_i == -101 or ('登录' in str(msg)) or ('权限' in str(msg))
                if should_alert:
                    upsert_system_alert(
                        category='BILIBILI_COOKIE_REQUIRED',
                        subject=domain,
                        level=3,
                        title=f'B站{source}接口 Cookie 失效或缺失',
                        content=f"B站{source}接口返回 code={code_i} {msg}，请更新 TargetAuth(domain={domain}) 的 cookie。"
                    )
                    return True

                upsert_system_alert(
                    category='BILIBILI_API_FAILED',
                    subject=domain,
                    level=2,
                    title=f'B站视频{source}接口异常',
                    content=f"B站{source}接口返回 code={code_i} {msg}\n{api}"
                )
                return True
        return False

    def _parse_dynamic_videos(self, payload):
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") or {}
        items = data.get("items") or []
        videos = []
        for item in items:
            if item.get("type") != "DYNAMIC_TYPE_AV":
                continue
            modules = item.get("modules") or {}
            module_dynamic = modules.get("module_dynamic") or {}
            major = module_dynamic.get("major") or {}
            if major.get("type") != "MAJOR_TYPE_ARCHIVE":
                continue
            archive = major.get("archive") or {}
            bvid = (archive.get("bvid") or "").strip()
            if not bvid:
                continue
            author = modules.get("module_author") or {}
            videos.append({
                "bvid": bvid,
                "title": archive.get("title") or "",
                "url": archive.get("jump_url") or f"https://www.bilibili.com/video/{bvid}",
                "cover_url": archive.get("cover") or archive.get("pic") or "",
                "author_name": author.get("name") or "",
                "published_ts": author.get("pub_ts"),
            })
        return videos

    def _parse_arc_videos(self, payload):
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") or {}
        lst = ((data.get("list") or {}).get("vlist")) or []
        videos = []
        for item in lst:
            bvid = (item.get("bvid") or "").strip()
            if not bvid:
                continue
            videos.append({
                "bvid": bvid,
                "title": item.get("title") or "",
                "url": f"https://www.bilibili.com/video/{bvid}",
                "cover_url": item.get("pic") or "",
                "author_name": item.get("author") or "",
                "published_ts": item.get("created"),
            })
        return videos

    def _parse_published_at(self, pub_ts):
        try:
            if pub_ts:
                dt = datetime.datetime.fromtimestamp(int(pub_ts), tz=datetime.timezone.utc)
                return dt.astimezone(timezone.get_current_timezone())
        except Exception:
            return None
        return None
