import re
import hashlib
import datetime
import time

import requests

from botend.controller.BaseScan import BaseScan
from botend.models import PortalEvent
from django.utils import timezone
from utils.log import logger


def _hash_url(url):
    u = (url or '').strip()
    if not u:
        return None
    return hashlib.sha256(u.encode('utf-8')).hexdigest()


class PortalEventMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        self.update_blizzard_cn()
        return True

    def update_blizzard_cn(self):
        src = "https://wow.blizzard.cn/news/"
        try:
            if self.req:
                driver = self.req.get(src, 'RespByChrome', 0, '', is_origin=1)
                if not driver:
                    return
                try:
                    driver.wait.doc_loaded()
                except Exception:
                    pass
                time.sleep(2)
                anchors = driver.eles('tag:a') or []
                seen = set()
                added = 0
                for a in anchors:
                    try:
                        url = (a.link or '').strip()
                    except Exception:
                        url = ''
                    if 'wow.blizzard.cn/news/' not in url:
                        continue
                    if url in seen:
                        continue
                    seen.add(url)
                    title = ''
                    day = ''
                    try:
                        t = a.ele('.list-title')
                        title = (t.text or '').strip() if t else ''
                    except Exception:
                        title = ''
                    try:
                        tt = a.ele('.list-time')
                        day = (tt.attr('data-time') or '').strip() if tt else ''
                    except Exception:
                        day = ''
                    title = re.sub(r'\\s+', ' ', title).strip()
                    if not title:
                        continue
                    start_at = None
                    if day:
                        try:
                            start_at = timezone.make_aware(datetime.datetime.strptime(day, "%Y-%m-%d"))
                        except Exception:
                            start_at = None
                    PortalEvent.objects.update_or_create(
                        url_hash=_hash_url(url),
                        defaults={
                            "title": title,
                            "url": url,
                            "source": "blizzard_cn",
                            "tag": "official",
                            "start_at": start_at,
                            "end_at": None,
                            "status": None,
                            "is_active": True,
                        },
                    )
                    added += 1
                    if added >= 20:
                        break
                return

            resp = requests.get(src, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return
            html = resp.text or ""
            seen = set()
            added = 0
            for m in re.finditer(r'href=\"(https?://wow\\.blizzard\\.cn/news/[^\"]+)\"', html):
                url = (m.group(1) or "").strip()
                if not url or url in seen:
                    continue
                block = html[m.start():m.start() + 2400]
                t = re.search(r'class=\"list-title\"[^>]*>(.*?)</div>', block, flags=re.S)
                d = re.search(r'class=\"list-time\"[^>]*data-time=\"([0-9\\-]+)\"', block, flags=re.S)
                title = (t.group(1) if t else "").strip()
                day = (d.group(1) if d else "").strip()
                title = re.sub(r'\\s+', ' ', title).strip()
                if not title:
                    continue
                seen.add(url)
                start_at = None
                if day:
                    try:
                        start_at = timezone.make_aware(datetime.datetime.strptime(day, "%Y-%m-%d"))
                    except Exception:
                        start_at = None
                PortalEvent.objects.update_or_create(
                    url_hash=_hash_url(url),
                    defaults={
                        "title": title,
                        "url": url,
                        "source": "blizzard_cn",
                        "tag": "official",
                        "start_at": start_at,
                        "end_at": None,
                        "status": None,
                        "is_active": True,
                    },
                )
                added += 1
                if added >= 20:
                    break
        except Exception as e:
            logger.error(f"[PortalEventMonitor] blizzard_cn error: {str(e)}")
