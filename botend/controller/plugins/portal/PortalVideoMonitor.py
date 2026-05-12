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


def _extract_mid(url):
    u = (url or '').strip()
    m = re.search(r"space\.bilibili\.com/(\d+)", u)
    if not m:
        return None
    return m.group(1)


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

        api = f"https://api.bilibili.com/x/space/arc/search?mid={mid}&pn=1&ps=20&order=pubdate"
        cookies = ""
        domain = urlparse(api).netloc
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

        payload = None
        fetch_error = ""
        try:
            if self.req:
                headers = {"Referer": target.target_url, "Accept": "application/json"}
                resp = self.req.get(api, 'Response', 0, cookies, headers)
                if not resp:
                    fetch_error = "接口请求失败"
                elif int(getattr(resp, "status_code", 0) or 0) != 200:
                    code = int(getattr(resp, "status_code", 0) or 0)
                    fetch_error = f"HTTP {code}"
                else:
                    raw = getattr(resp, "text", "") or ""
                    if raw.strip():
                        payload = json.loads(raw)
                    else:
                        fetch_error = "接口返回为空"
            else:
                import requests
                headers = {"User-Agent": "Mozilla/5.0", "Referer": target.target_url, "Cookie": cookies}
                resp = requests.get(api, timeout=20, headers=headers)
                if resp.status_code != 200:
                    fetch_error = f"HTTP {resp.status_code}"
                else:
                    payload = resp.json() or {}
        except Exception as e:
            fetch_error = str(e)
            payload = None

        if not isinstance(payload, dict) or not payload:
            if fetch_error:
                try:
                    if 'HTTP 412' in fetch_error:
                        upsert_system_alert(
                            category='BILIBILI_COOKIE_REQUIRED',
                            subject=domain,
                            level=3,
                            title='B站接口被风控(412)',
                            content=f"接口返回 {fetch_error}，通常需要配置 cookie 才能稳定访问。\n请更新 TargetAuth(domain={domain}) 或 TargetAuth(domain=bilibili.com) 的 cookie。\n{api}"
                        )
                        return
                    upsert_system_alert(
                        category='BILIBILI_API_FAILED',
                        subject=domain,
                        level=2,
                        title='B站视频接口请求失败',
                        content=f"{fetch_error}\n{api}"
                    )
                except Exception:
                    pass
            return

        code = payload.get('code')
        msg = payload.get('message') or payload.get('msg') or ''
        if code is not None:
            try:
                code_i = int(code)
            except Exception:
                code_i = None
            if code_i is not None and code_i != 0:
                is_rate_limited = code_i == -799 or ('频繁' in str(msg)) or ('稍后再试' in str(msg))
                if is_rate_limited:
                    upsert_system_alert(
                        category='BILIBILI_RATE_LIMIT',
                        subject=domain,
                        level=2,
                        title='B站接口请求过于频繁',
                        content=f"B站接口返回 code={code_i} {msg}，请降低视频监控频率或配置 TargetAuth(domain={domain}) 的 cookie。"
                    )
                    return

                should_alert = code_i == -101 or ('登录' in str(msg)) or ('权限' in str(msg))
                if should_alert:
                    upsert_system_alert(
                        category='BILIBILI_COOKIE_REQUIRED',
                        subject=domain,
                        level=3,
                        title='B站 Cookie 失效或缺失',
                        content=f"B站接口返回 code={code_i} {msg}，请更新 TargetAuth(domain={domain}) 的 cookie。"
                    )
                    return

                upsert_system_alert(
                    category='BILIBILI_API_FAILED',
                    subject=domain,
                    level=2,
                    title='B站视频接口异常',
                    content=f"B站接口返回 code={code_i} {msg}\n{api}"
                )
                return
        data = payload.get("data") or {}
        lst = ((data.get("list") or {}).get("vlist")) or []
        if not lst:
            return

        newest_bvid = None
        for item in lst:
            bvid = (item.get("bvid") or "").strip()
            if not bvid:
                continue
            if not newest_bvid:
                newest_bvid = bvid
            if target.last_seen_bvid and bvid == target.last_seen_bvid:
                break
            title = (item.get("title") or "").strip()
            cover = (item.get("pic") or "").strip()
            author = (item.get("author") or "").strip()
            pub_ts = item.get("created")
            published_at = None
            try:
                if pub_ts:
                    dt = datetime.datetime.fromtimestamp(int(pub_ts), tz=timezone.utc)
                    published_at = dt.astimezone(timezone.get_current_timezone())
            except Exception:
                published_at = None
            video_url = f"https://www.bilibili.com/video/{bvid}"

            PortalVideo.objects.update_or_create(
                url=video_url,
                defaults={
                    "title": title or bvid,
                    "bvid": bvid,
                    "cover_url": cover or None,
                    "published_at": published_at,
                    "author_name": author or target.name,
                    "author_url": target.target_url or f"https://space.bilibili.com/{mid}",
                    "tag": target.tag,
                    "target": target,
                    "is_active": True,
                },
            )

        if newest_bvid and newest_bvid != target.last_seen_bvid:
            target.last_seen_bvid = newest_bvid
            target.save(update_fields=["last_seen_bvid"])
