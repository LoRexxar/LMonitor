import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from botend.models import PortalEvent


DEFAULT_EVENT_SOURCES = [
    {
        "source": "blizzard_cn",
        "tag": "官方活动",
        "url": "https://wow.blizzard.cn/news/",
    },
    {
        "source": "blizzard_us",
        "tag": "官方活动",
        "url": "https://worldofwarcraft.blizzard.com/en-us/news",
    },
]

EVENT_TITLE_KEYWORDS = [
    "活动",
    "节日",
    "赛季",
    "周年",
    "乱斗",
    "时空漫游",
    "暗月",
    "假日",
    "event",
    "events",
    "holiday",
    "bonus",
    "timewalking",
    "brawl",
    "anniversary",
    "season",
]

DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})[年\-/\.](?P<m>\d{1,2})[月\-/\.](?P<d>\d{1,2})日?"),
    re.compile(r"(?P<m>\d{1,2})[月\-/\.](?P<d>\d{1,2})日?"),
]


@dataclass
class ParsedPortalEvent:
    title: str
    url: str
    source: str = "unknown"
    tag: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str = ""
    summary: str = ""
    image_url: str = ""
    external_id: str = ""
    raw_data: dict | None = None


def hash_url(url):
    value = (url or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(timezone.get_current_timezone())


def parse_dates_from_text(text):
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return None, None
    matches = []
    now = timezone.now()
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(value):
            try:
                year = int(match.groupdict().get("y") or now.year)
                month = int(match.group("m"))
                day = int(match.group("d"))
                dt = timezone.make_aware(datetime(year, month, day), timezone.get_current_timezone())
                matches.append((match.start(), dt))
            except Exception:
                continue
        if matches:
            break
    matches.sort(key=lambda item: item[0])
    if not matches:
        return None, None
    start_at = matches[0][1]
    end_at = matches[1][1] if len(matches) > 1 else None
    if end_at and end_at < start_at:
        end_at = end_at.replace(year=end_at.year + 1)
    return start_at, end_at


def infer_status(start_at, end_at, explicit_status=""):
    value = (explicit_status or "").strip()
    if value:
        return value
    now = timezone.now()
    if end_at and now > end_at:
        return "已结束"
    if start_at and now < start_at:
        return "即将开始"
    if start_at:
        return "进行中"
    return ""


class PortalEventService:
    def __init__(self, request_client=None, sources=None):
        self.request_client = request_client
        self.sources = sources or DEFAULT_EVENT_SOURCES
        req_cfg = getattr(settings, "REQUEST_CONFIG", {}) or {}
        self._proxies = req_cfg.get("proxies") if req_cfg.get("enable_proxy", False) else None

    def sync_events(self, source_url="", deactivate_missing=False):
        parsed = self.collect_events(source_url=source_url)
        seen_hashes = set()
        created = 0
        updated = 0
        for item in parsed:
            obj, was_created = self.upsert_event(item)
            if obj:
                seen_hashes.add(obj.url_hash)
                if was_created:
                    created += 1
                else:
                    updated += 1
        deactivated = 0
        if deactivate_missing and seen_hashes:
            deactivated = PortalEvent.objects.exclude(url_hash__in=seen_hashes).update(is_active=False)
        return {
            "total": len(parsed),
            "created": created,
            "updated": updated,
            "deactivated": deactivated,
        }

    def collect_events(self, source_url=""):
        sources = self._resolve_sources(source_url)
        results = []
        seen = set()
        for source in sources:
            html_text = self._fetch_html(source["url"])
            for item in self.parse_html(html_text, source_url=source["url"], source=source["source"], tag=source["tag"]):
                key = item.external_id or item.url
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
        return results

    def _resolve_sources(self, source_url):
        value = (source_url or "").strip()
        if not value:
            return self.sources
        netloc = urlparse(value).netloc.lower()
        source = "blizzard_cn" if "blizzard.cn" in netloc else "blizzard_us" if "blizzard.com" in netloc else "custom"
        return [{"source": source, "tag": "官方活动", "url": value}]

    def _fetch_html(self, url):
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if self.request_client:
            try:
                resp = self.request_client.get(url, "Response", 0, "", headers=headers)
                if resp and int(getattr(resp, "status_code", 0) or 0) == 200:
                    return getattr(resp, "text", "") or ""
            except Exception:
                return ""
            return ""
        try:
            resp = requests.get(url, timeout=25, headers=headers, proxies=self._proxies)
            if resp.status_code == 200:
                return resp.text or ""
        except Exception:
            return ""
        return ""

    def parse_html(self, html_text, source_url, source="unknown", tag=""):
        if not html_text:
            return []
        soup = BeautifulSoup(html_text, "html.parser")
        items = []
        for node in soup.find_all(["article", "li", "a", "div"]):
            item = self._parse_node(node, source_url=source_url, source=source, tag=tag)
            if item:
                items.append(item)
        return self._dedupe_and_limit(items)

    def _parse_node(self, node, source_url, source, tag):
        link_node = node if node.name == "a" and node.get("href") else node.find("a", href=True)
        if not link_node:
            return None
        title = self._extract_title(node, link_node)
        if not self._looks_like_event(title):
            return None
        url = urljoin(source_url, link_node.get("href") or "")
        if not url.startswith("http"):
            return None
        text = node.get_text(" ", strip=True)
        start_at, end_at = parse_dates_from_text(text)
        image = node.find("img")
        image_url = ""
        if image:
            image_url = urljoin(source_url, image.get("src") or image.get("data-src") or "")
        summary = self._extract_summary(text, title)
        return ParsedPortalEvent(
            title=title,
            url=url,
            source=source,
            tag=tag,
            start_at=start_at,
            end_at=end_at,
            status=infer_status(start_at, end_at),
            summary=summary,
            image_url=image_url,
            external_id=hash_url(url),
            raw_data={"source_url": source_url, "text": text[:1000]},
        )

    def _extract_title(self, node, link_node):
        for selector in ["h1", "h2", "h3", "h4", "[class*=title]", "[class*=Title]"]:
            title_node = node.select_one(selector)
            if title_node:
                text = title_node.get_text(" ", strip=True)
                if text:
                    return re.sub(r"\s+", " ", text).strip()
        text = link_node.get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    def _extract_summary(self, text, title):
        value = re.sub(r"\s+", " ", text or "").strip()
        if title and value.startswith(title):
            value = value[len(title):].strip()
        return value[:500]

    def _looks_like_event(self, title):
        value = (title or "").strip()
        if len(value) < 3 or len(value) > 180:
            return False
        lower = value.lower()
        return any(keyword.lower() in lower for keyword in EVENT_TITLE_KEYWORDS)

    def _dedupe_and_limit(self, items, limit=80):
        results = []
        seen = set()
        for item in items:
            key = item.external_id or hash_url(item.url)
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def upsert_event(self, item):
        if not item or not item.url or not item.title:
            return None, False
        url_hash = item.external_id or hash_url(item.url)
        defaults = {
            "title": item.title[:500],
            "url": item.url[:2000],
            "url_hash": url_hash,
            "source": (item.source or "unknown")[:32],
            "tag": (item.tag or "")[:64],
            "start_at": item.start_at,
            "end_at": item.end_at,
            "status": infer_status(item.start_at, item.end_at, item.status)[:32] or None,
            "is_active": True,
            "summary": item.summary or "",
            "image_url": (item.image_url or "")[:2000],
            "external_id": (item.external_id or url_hash)[:128],
            "raw_data": item.raw_data or {},
            "last_seen_at": timezone.now(),
        }
        return PortalEvent.objects.update_or_create(url_hash=url_hash, defaults=defaults)

    def seed_fallback_events(self):
        now = timezone.now()
        data = [
            ParsedPortalEvent(
                title="暗月马戏团",
                url="https://worldofwarcraft.blizzard.com/zh-cn/news?event=darkmoon-faire",
                source="seed",
                tag="周期活动",
                start_at=now + timedelta(days=1),
                end_at=now + timedelta(days=8),
                summary="周期性世界活动，占位数据会被后续官方采集结果刷新。",
            ),
            ParsedPortalEvent(
                title="时空漫游周",
                url="https://worldofwarcraft.blizzard.com/zh-cn/news?event=timewalking",
                source="seed",
                tag="周期活动",
                start_at=now,
                end_at=now + timedelta(days=7),
                summary="周期性地下城活动，占位数据会被后续官方采集结果刷新。",
            ),
        ]
        created = 0
        updated = 0
        for item in data:
            item.external_id = hash_url(item.url)
            item.status = infer_status(item.start_at, item.end_at)
            _, was_created = self.upsert_event(item)
            if was_created:
                created += 1
            else:
                updated += 1
        return {"total": len(data), "created": created, "updated": updated}


def dumps_raw_data(value):
    return json.dumps(value or {}, ensure_ascii=False, default=str)
