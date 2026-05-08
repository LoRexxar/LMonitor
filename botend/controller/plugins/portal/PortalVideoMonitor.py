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
        return True

    def update_target(self, target):
        mid = _extract_mid(target.target_url)
        if not mid:
            return

        api = f"https://api.bilibili.com/x/space/arc/search?mid={mid}&pn=1&ps=20&order=pubdate"
        cookies = ""
        try:
            domain = urlparse(api).netloc
            auth = TargetAuth.objects.filter(domain=domain).first()
            if auth and auth.cookie:
                cookies = auth.cookie
        except Exception:
            cookies = ""

        payload = {}
        try:
            if self.req:
                if getattr(self.req, 'is_chrome', False):
                    raw = self.req.get(api, 'RespByChrome', 0, cookies)
                else:
                    raw = self.req.get(api, 'Resp', 0, cookies)
                if not raw:
                    return
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode('utf-8', errors='ignore')
                payload = json.loads(raw)
            else:
                import requests
                headers = {"User-Agent": "Mozilla/5.0", "Referer": target.target_url, "Cookie": cookies}
                resp = requests.get(api, timeout=20, headers=headers)
                if resp.status_code != 200:
                    return
                payload = resp.json() or {}
        except Exception:
            payload = {}

        try:
            code = payload.get('code')
            msg = payload.get('message') or payload.get('msg') or ''
            if code is not None and int(code) != 0:
                domain = urlparse(api).netloc
                should_alert = int(code) == -101 or ('登录' in str(msg)) or ('权限' in str(msg))
                if should_alert:
                    upsert_system_alert(
                        category='BILIBILI_COOKIE_REQUIRED',
                        subject=domain,
                        level=3,
                        title='B站 Cookie 失效或缺失',
                        content=f"B站接口返回 code={code} {msg}，请更新 TargetAuth(domain={domain}) 的 cookie。"
                    )
                return
        except Exception:
            pass
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
