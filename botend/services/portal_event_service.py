import csv
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
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

WAGO_DB2_INDEX_URL = "https://wago.tools/db2"
WAGO_DB2_CSV_URL = "https://wago.tools/db2/{table}/csv?build={build}&locale={locale}"
WAGO_DB2_EVENT_TABLES = ("Holidays", "HolidayNames", "HolidayDescriptions")
WAGO_DB2_GLOBAL_REGION = "0"
WAGO_DB2_CN_REGION = "2"
WAGO_DB2_ALLOWED_REGIONS = {WAGO_DB2_GLOBAL_REGION, WAGO_DB2_CN_REGION}
WAGO_DB2_LOOPING_HORIZON_DAYS = 730
WOWHEAD_EVENTS_URL = "https://www.wowhead.com/events"
WOWHEAD_HOME_URL = "https://www.wowhead.com/"
WOWHEAD_CN_SHIFT_DAYS = 2
WOWHEAD_CN_START_HOUR = 8
WOWHEAD_EVENT_HORIZON_DAYS = 120

WOWHEAD_EVENT_TITLE_MAP = {
    "arena skirmish bonus event": "竞技场练习赛假日活动",
    "battleground bonus event": "战场假日活动",
    "brewfest": "美酒节",
    "children's week": "儿童周",
    "darkmoon faire": "暗月马戏团",
    "delves bonus event": "地下堡奖励活动",
    "feast of winter veil": "冬幕节",
    "hallow's end": "万圣节",
    "lunar festival": "春节",
    "midsummer fire festival": "仲夏火焰节",
    "noblegarden": "复活节",
    "pet battle bonus event": "宠物对战假日活动",
    "pilgrim's bounty": "感恩节",
    "pirates' day": "海盗日",
    "timewalking dungeon event": "时空漫游地下城活动",
    "world quest bonus event": "世界任务奖励活动",
}

WOWHEAD_EVENT_TITLE_PATTERNS = [
    (re.compile(r"^(?P<name>.+) timewalking dungeon event$", re.I), "{name} 时空漫游地下城活动"),
    (re.compile(r"^pvp brawl:\s*(?P<name>.+)$", re.I), "PvP 乱斗：{name}"),
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
        normalized = text.replace("/", "-") if re.match(r"^\d{4}/\d{2}/\d{2}", text) else text
        try:
            dt = datetime.fromisoformat(normalized)
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
        if source_url:
            return self.sync_news_events(source_url=source_url, deactivate_missing=deactivate_missing)
        return self.sync_wowhead_events(deactivate_missing=deactivate_missing)

    def sync_news_events(self, source_url="", deactivate_missing=False):
        parsed = self.collect_events(source_url=source_url)
        return self.upsert_events(parsed, deactivate_missing=deactivate_missing)

    def sync_wowhead_events(self, source_url="", deactivate_missing=False):
        parsed = self.collect_wowhead_events(source_url=source_url)
        return self.upsert_events(parsed, deactivate_missing=deactivate_missing)

    def sync_db2_events(self, build="", locale="zhCN", deactivate_missing=False):
        parsed = self.collect_db2_events(build=build, locale=locale)
        return self.upsert_events(parsed, deactivate_missing=deactivate_missing)

    def upsert_events(self, parsed, deactivate_missing=False):
        payloads = []
        seen_hashes = set()
        for item in parsed:
            payload = self._build_event_defaults(item)
            if not payload:
                continue
            url_hash = payload["url_hash"]
            if url_hash in seen_hashes:
                continue
            seen_hashes.add(url_hash)
            payloads.append(payload)

        existing = PortalEvent.objects.in_bulk(seen_hashes, field_name="url_hash") if seen_hashes else {}
        to_create = []
        to_update = []
        update_fields = [
            "title", "url", "source", "tag", "start_at", "end_at", "status", "is_active",
            "summary", "image_url", "external_id", "raw_data", "last_seen_at",
        ]
        for payload in payloads:
            obj = existing.get(payload["url_hash"])
            if obj:
                for field in update_fields:
                    setattr(obj, field, payload[field])
                to_update.append(obj)
            else:
                to_create.append(PortalEvent(**payload))

        if to_create:
            PortalEvent.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
        if to_update:
            PortalEvent.objects.bulk_update(to_update, update_fields, batch_size=500)

        deactivated = 0
        if deactivate_missing and seen_hashes:
            deactivated = PortalEvent.objects.exclude(url_hash__in=seen_hashes).update(is_active=False)
        return {
            "total": len(payloads),
            "created": len(to_create),
            "updated": len(to_update),
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

    def collect_wowhead_events(self, source_url=""):
        urls = [source_url] if source_url else [WOWHEAD_EVENTS_URL, WOWHEAD_HOME_URL]
        results = []
        seen = set()
        for url in urls:
            html_text = self._fetch_html(url)
            for item in self.parse_wowhead_events(html_text, source_url=url):
                key = item.external_id or item.url
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
        return self._dedupe_and_limit(sorted(results, key=lambda item: item.start_at or timezone.now()), limit=500)

    def parse_wowhead_events(self, html_text, source_url=WOWHEAD_EVENTS_URL):
        if not html_text:
            return []
        items = []
        for raw_item in self._extract_wowhead_raw_events(html_text):
            title = self._normalize_wowhead_title(raw_item.get("name") or raw_item.get("title") or "")
            if not title or not self._looks_like_event(title):
                continue
            for occurrence in self._iter_wowhead_occurrences(raw_item):
                original_start = occurrence["start_at"]
                original_end = occurrence["end_at"]
                if not original_start:
                    continue
                start_at, end_at = self._shift_wowhead_event_time(original_start, original_end)
                now = timezone.now()
                cutoff = now - timedelta(days=7)
                if (end_at or start_at) < cutoff:
                    continue
                if start_at > now + timedelta(days=WOWHEAD_EVENT_HORIZON_DAYS):
                    continue
                url = urljoin(source_url, raw_item.get("url") or raw_item.get("iconUrl") or "/events")
                raw_url = raw_item.get("url") or ""
                event_id = self._extract_wowhead_event_id(raw_url) or raw_item.get("id") or title
                external_source = f"wowhead-cn-{event_id}-{int(start_at.timestamp())}"
                items.append(ParsedPortalEvent(
                    title=title,
                    url=url,
                    source="wowhead_cn_derived",
                    tag="日历活动",
                    start_at=start_at,
                    end_at=end_at,
                    status=infer_status(start_at, end_at),
                    summary="Wowhead 日历数据按国服时间平移 2 天生成。",
                    image_url=self._normalize_wowhead_icon_url(raw_item.get("icon") or raw_item.get("wowIcon") or ""),
                    external_id=hash_url(external_source),
                    raw_data={
                        "source": "wowhead",
                        "source_url": source_url,
                        "original_title": raw_item.get("name") or raw_item.get("title") or "",
                        "original_start_at": original_start.isoformat(),
                        "original_end_at": original_end.isoformat() if original_end else "",
                        "time_shift_days": WOWHEAD_CN_SHIFT_DAYS,
                        "cn_start_hour": WOWHEAD_CN_START_HOUR,
                        "raw": raw_item,
                    },
                ))
        return self._dedupe_and_limit(items, limit=500)

    def _iter_wowhead_occurrences(self, raw_item):
        occurrences = raw_item.get("occurrences") or []
        if isinstance(occurrences, list) and occurrences:
            for occurrence in occurrences:
                if not isinstance(occurrence, dict):
                    continue
                start_at = parse_datetime_value(occurrence.get("start"))
                end_at = parse_datetime_value(occurrence.get("end"))
                if start_at:
                    yield {"start_at": start_at, "end_at": end_at}
            return
        start_at = self._parse_wowhead_timestamp(raw_item.get("startingUt") or raw_item.get("start") or raw_item.get("startUt"))
        end_at = self._parse_wowhead_timestamp(raw_item.get("endingUt") or raw_item.get("end") or raw_item.get("endUt"))
        if start_at:
            yield {"start_at": start_at, "end_at": end_at}

    def _extract_wowhead_raw_events(self, html_text):
        raw_items = []
        for body in self._extract_wowhead_script_bodies(html_text):
            raw_items.extend(self._extract_wowhead_json_items(body))
        raw_items.extend(self._extract_wowhead_json_items(html_text))
        return raw_items

    def _extract_wowhead_script_bodies(self, html_text):
        bodies = []
        for match in re.finditer(r"<script[^>]*>(.*?)</script>", html_text or "", flags=re.S | re.I):
            body = html.unescape(match.group(1) or "")
            if any(token in body for token in ("startingUt", "endingUt", "Bonus Event", "World Event", "Darkmoon Faire")):
                bodies.append(body)
        return bodies

    def _extract_wowhead_json_items(self, text):
        items = []
        for data_text in self._extract_wowhead_listview_data(text):
            try:
                obj = json.loads(data_text)
            except Exception:
                continue
            self._collect_wowhead_event_objects(obj, items)
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text or ""):
            try:
                obj, _ = decoder.raw_decode(text[match.start():])
            except Exception:
                continue
            self._collect_wowhead_event_objects(obj, items)
        return items

    def _extract_wowhead_listview_data(self, text):
        results = []
        for match in re.finditer(r"\bdata\s*:\s*\[", text or ""):
            start = match.end() - 1
            depth = 0
            in_string = False
            escape = False
            quote = ""
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == quote:
                        in_string = False
                    continue
                if char in {'\"', "'"}:
                    in_string = True
                    quote = char
                    continue
                if char == "[":
                    depth += 1
                elif char == "]":
                    depth -= 1
                    if depth == 0:
                        results.append(text[start:index + 1])
                        break
        return results

    def _collect_wowhead_event_objects(self, obj, items):
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("title")
            has_time = any(obj.get(key) for key in ("startingUt", "endingUt", "start", "startUt", "end", "endUt")) or bool(obj.get("occurrences"))
            if name and has_time:
                items.append(obj)
            for value in obj.values():
                self._collect_wowhead_event_objects(value, items)
        elif isinstance(obj, list):
            for value in obj:
                self._collect_wowhead_event_objects(value, items)

    def _parse_wowhead_timestamp(self, value):
        if value in (None, ""):
            return None
        try:
            timestamp = int(value)
        except Exception:
            return parse_datetime_value(value)
        if timestamp > 100000000000:
            timestamp = int(timestamp / 1000)
        try:
            return datetime.fromtimestamp(timestamp, tz=dt_timezone.utc).astimezone(timezone.get_current_timezone())
        except Exception:
            return None

    def _shift_wowhead_event_time(self, original_start, original_end=None):
        start_at = (original_start + timedelta(days=WOWHEAD_CN_SHIFT_DAYS)).astimezone(timezone.get_current_timezone())
        start_at = start_at.replace(hour=WOWHEAD_CN_START_HOUR, minute=0, second=0, microsecond=0)
        end_at = None
        if original_end:
            duration = original_end - original_start
            end_at = start_at + duration
        return start_at, end_at

    def _normalize_wowhead_title(self, title):
        value = re.sub(r"\s+", " ", html.unescape(str(title or ""))).strip()
        lower = value.lower()
        if lower in WOWHEAD_EVENT_TITLE_MAP:
            return WOWHEAD_EVENT_TITLE_MAP[lower]
        for pattern, template in WOWHEAD_EVENT_TITLE_PATTERNS:
            match = pattern.match(value)
            if match:
                return template.format(**match.groupdict())
        return value

    def _extract_wowhead_event_id(self, url):
        match = re.search(r"/event=(\d+)", str(url or ""))
        return match.group(1) if match else ""

    def _normalize_wowhead_icon_url(self, icon):
        value = str(icon or "").strip()
        if not value:
            return ""
        if value.startswith("http"):
            return value
        return f"https://wow.zamimg.com/images/wow/icons/large/{value}.jpg"

    def collect_db2_events(self, build="", locale="zhCN"):
        build = (build or "").strip() or self._fetch_current_wago_build()
        locale = (locale or "zhCN").strip() or "zhCN"
        tables = {table: self._fetch_wago_csv_rows(table, build, locale) for table in WAGO_DB2_EVENT_TABLES}
        return self.parse_db2_holidays(
            tables.get("Holidays") or [],
            tables.get("HolidayNames") or [],
            tables.get("HolidayDescriptions") or [],
            build=build,
            locale=locale,
        )

    def parse_db2_holidays(self, holiday_rows, name_rows, description_rows, build="", locale="zhCN"):
        names = {str(row.get("ID") or ""): (row.get("Name_lang") or "").strip() for row in name_rows}
        descriptions = {
            str(row.get("ID") or ""): (row.get("Description_lang") or "").strip()
            for row in description_rows
        }
        events = []
        for row in holiday_rows:
            region = str(row.get("Region") or "").strip()
            if region not in WAGO_DB2_ALLOWED_REGIONS:
                continue
            holiday_id = str(row.get("ID") or "").strip()
            title = names.get(str(row.get("HolidayNameID") or ""), "").strip()
            if not holiday_id or not title:
                continue
            summary = descriptions.get(str(row.get("HolidayDescriptionID") or ""), "").strip()
            occurrences = self._extract_db2_occurrences(row)
            if not occurrences:
                continue
            texture_ids = [
                int(row.get(f"TextureFileDataID_{index}") or 0)
                for index in range(3)
                if int(row.get(f"TextureFileDataID_{index}") or 0)
            ]
            for occurrence_index, occurrence in enumerate(occurrences):
                start_at = occurrence["start_at"]
                duration_hours = occurrence["duration_hours"]
                end_at = start_at + timedelta(hours=duration_hours) if duration_hours else None
                external_id = f"db2-holiday-{holiday_id}-{occurrence_index}-{int(start_at.timestamp())}"
                events.append(ParsedPortalEvent(
                    title=title,
                    url=f"https://wago.tools/db2/Holidays?build={build}&locale={locale}&filter%5BID%5D=exact%3A{holiday_id}",
                    source="db2_holidays",
                    tag="日历活动",
                    start_at=start_at,
                    end_at=end_at,
                    status=infer_status(start_at, end_at),
                    summary=summary,
                    external_id=hash_url(external_id),
                    raw_data={
                        "source": "wago_db2",
                        "build": build,
                        "locale": locale,
                        "table": "Holidays",
                        "holiday_id": holiday_id,
                        "region": region,
                        "region_scope": "global" if region == WAGO_DB2_GLOBAL_REGION else "cn",
                        "date_index": occurrence["date_index"],
                        "occurrence_index": occurrence_index,
                        "is_looping": occurrence["is_looping"],
                        "loop_interval_hours": occurrence["loop_interval_hours"],
                        "duration_hours": duration_hours,
                        "texture_file_data_ids": texture_ids,
                        "row": row,
                    },
                ))
        return self._dedupe_and_limit(events, limit=5000)

    def _fetch_current_wago_build(self):
        html_text = self._fetch_html(WAGO_DB2_INDEX_URL)
        props = self._extract_wago_props(html_text)
        build = (props.get("currentVersion") or "").strip()
        return build or "latest"

    def _fetch_wago_csv_rows(self, table, build, locale):
        url = WAGO_DB2_CSV_URL.format(table=table, build=build, locale=locale)
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*;q=0.8"}
        try:
            resp = requests.get(url, timeout=60, headers=headers, proxies=self._proxies)
            if resp.status_code != 200:
                return []
        except Exception:
            return []
        return list(csv.DictReader(StringIO(resp.text or "")))

    def _extract_wago_props(self, html_text):
        match = re.search(r"data-page=(?:\"([^\"]+)\"|'([^']+)')", html_text or "")
        if not match:
            return {}
        raw = match.group(1) or match.group(2) or ""
        try:
            payload = json.loads(html.unescape(raw))
        except Exception:
            return {}
        return payload.get("props") or {}

    def _extract_db2_durations(self, row):
        durations = []
        for index in range(10):
            try:
                value = int(row.get(f"Duration_{index}") or 0)
            except Exception:
                value = 0
            if value > 0:
                durations.append(value)
        return durations

    def _extract_db2_occurrences(self, row):
        dates = self._extract_db2_dates(row)
        if not dates:
            return []
        durations = self._extract_db2_durations(row)
        duration_hours = durations[0] if durations else 0
        loop_interval_hours = durations[1] if len(durations) > 1 else 0
        is_looping = self._is_truthy_db2_value(row.get("Looping")) and loop_interval_hours > 0
        horizon_end = timezone.now() + timedelta(days=WAGO_DB2_LOOPING_HORIZON_DAYS)
        occurrences = []
        seen_starts = set()
        for date_index, base_start in enumerate(dates):
            start_at = base_start
            while True:
                if start_at not in seen_starts:
                    seen_starts.add(start_at)
                    occurrences.append({
                        "start_at": start_at,
                        "date_index": date_index,
                        "duration_hours": duration_hours,
                        "is_looping": is_looping,
                        "loop_interval_hours": loop_interval_hours if is_looping else 0,
                    })
                if not is_looping:
                    break
                start_at = start_at + timedelta(hours=loop_interval_hours)
                if start_at > horizon_end:
                    break
        occurrences.sort(key=lambda item: item["start_at"])
        return occurrences

    def _is_truthy_db2_value(self, value):
        return str(value or "").strip().lower() in {"1", "true", "yes"}

    def _extract_db2_dates(self, row):
        dates = []
        for index in range(26):
            try:
                value = int(row.get(f"Date_{index}") or 0)
            except Exception:
                value = 0
            parsed = self._decode_db2_calendar_time(value)
            if parsed:
                dates.append(parsed)
        dates.sort()
        return dates

    def _decode_db2_calendar_time(self, value):
        if not value:
            return None
        try:
            minute = value & 0x3F
            hour = (value >> 6) & 0x1F
            day = ((value >> 14) & 0x3F) + 1
            month = ((value >> 20) & 0x0F) + 1
            year = ((value >> 24) & 0x1F) + 2000
            dt = datetime(year, month, day, hour, minute)
        except Exception:
            return None
        return timezone.make_aware(dt, timezone.get_current_timezone())

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
        defaults = self._build_event_defaults(item)
        if not defaults:
            return None, False
        return PortalEvent.objects.update_or_create(url_hash=defaults["url_hash"], defaults=defaults)

    def _build_event_defaults(self, item):
        if not item or not item.url or not item.title:
            return None
        url_hash = item.external_id or hash_url(item.url)
        return {
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
