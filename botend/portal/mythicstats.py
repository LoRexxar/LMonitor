import re
from urllib.parse import urlencode

import requests
from django.utils import timezone

from botend.models import PortalCache, PortalMythicstatsDpsRow
from utils.log import logger


def _parse_season(value):
    v = (value or "").strip()
    if not v:
        return ""
    if v in {"season-mn-1", "auto"}:
        return ""
    if "season=" in v:
        m = re.search(r"season=([^&]+)", v)
        if m:
            return (m.group(1) or "").strip()
    return v


def _parse_suffix_number(s):
    t = (s or "").strip().replace(",", "")
    if not t:
        return None
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([kKmM]?)$", t)
    if not m:
        try:
            return float(t)
        except Exception:
            return None
    v = float(m.group(1))
    suf = (m.group(2) or "").lower()
    if suf == "k":
        v *= 1000
    elif suf == "m":
        v *= 1000000
    return v


def _parse_diff(s):
    raw = (s or "").strip()
    if not raw:
        return "", None
    raw = raw.replace("\xa0", " ").strip()
    if raw in {"0", "—", "-"}:
        return raw, 0
    m = re.search(r"([0-9]+)", raw)
    if not m:
        return raw, None
    n = int(m.group(1))
    if "↓" in raw or "-" in raw:
        return raw, -n
    if "↑" in raw or "+" in raw:
        return raw, n
    return raw, n


def _extract_slug_from_spec_url(url):
    u = (url or "").strip()
    m = re.search(r"/spec/([^/?#]+)", u)
    if m:
        return (m.group(1) or "").strip()
    return ""


def _extract_week_from_label(label):
    t = (label or "").strip()
    m = re.search(r"week\s*([0-9]+)", t, flags=re.I)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def _parse_season_from_period_html(html):
    t = (html or "").strip()
    if not t:
        return "", ""
    m = re.search(r"week\s*[0-9]+\s*of\s*\[([^\]]+)\]\(https://mythicstats\.com/season/([^)]+)\)", t, flags=re.I)
    if m:
        return (m.group(2) or "").strip(), (m.group(1) or "").strip()
    m = re.search(r"https://mythicstats\.com/season/([a-zA-Z0-9_-]+)", t)
    if m:
        return (m.group(1) or "").strip(), ""
    return "", ""


def fetch_period_season_slug(*, req=None, period_id=None):
    if not period_id:
        return "", ""
    html = _get_html(req, f"https://mythicstats.com/period/{int(period_id)}")
    return _parse_season_from_period_html(html)


def fetch_current_season_slug(*, req=None):
    html = _get_html(req, "https://mythicstats.com/period/latest")
    return _parse_season_from_period_html(html)


def _get_html(req, url):
    try:
        if req:
            resp = req.get(url, "Response", 0, "", headers={"User-Agent": "Mozilla/5.0"})
            if resp and getattr(resp, "status_code", 0) == 200 and (resp.text or "").strip():
                return resp.text
            if getattr(req, "is_chrome", False):
                driver = req.get(url, "RespByChrome", 0, "", is_origin=1)
                if driver and getattr(driver, "html", None):
                    return driver.html
        resp = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.error(f"[mythicstats] fetch html error: {str(e)}")
    return ""


def _parse_meta_from_html(html):
    try:
        from bs4 import BeautifulSoup
    except Exception:
        BeautifulSoup = None

    if not html:
        return {"dungeons": [], "periods": []}

    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        selects = soup.find_all("select") or []

        def parse_select(sel):
            items = []
            for opt in sel.find_all("option") or []:
                label = (opt.get_text() or "").strip()
                val = (opt.get("value") or "").strip()
                items.append({"value": val, "label": label})
            return items

        dungeon_sel = None
        period_sel = None
        for sel in selects:
            txt = " ".join([(o.get_text() or "").strip() for o in sel.find_all("option")[:6]])
            if not dungeon_sel and re.search(r"All dungeons", txt, flags=re.I):
                dungeon_sel = sel
            if not period_sel and re.search(r"week", txt, flags=re.I):
                period_sel = sel

        dungeons = []
        if dungeon_sel:
            for it in parse_select(dungeon_sel):
                if not it["label"]:
                    continue
                v = it["value"]
                try:
                    vid = int(v) if str(v).isdigit() else 0
                except Exception:
                    vid = 0
                if it["label"].lower().startswith("all dungeons"):
                    vid = 0
                dungeons.append({"id": vid, "name": it["label"]})

        periods = []
        if period_sel:
            for it in parse_select(period_sel):
                if not it["label"]:
                    continue
                v = it["value"]
                try:
                    pid = int(v) if str(v).isdigit() else int(re.search(r"([0-9]+)", it["label"]).group(1))
                except Exception:
                    continue
                periods.append({"id": pid, "label": it["label"]})

        if periods:
            periods.sort(key=lambda x: x["id"], reverse=True)
        if dungeons:
            seen = set()
            uniq = []
            for d in dungeons:
                k = int(d["id"])
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(d)
            dungeons = uniq
        return {"dungeons": dungeons, "periods": periods}

    text = html
    dungeons = [{"id": 0, "name": "All dungeons"}]
    periods = []
    for m in re.finditer(r"([0-9]{4})\s*\(week\s*([0-9]+)\)", text, flags=re.I):
        pid = int(m.group(1))
        label = f"{pid} (week {m.group(2)})"
        periods.append({"id": pid, "label": label})
    periods.sort(key=lambda x: x["id"], reverse=True)
    uniq_periods = []
    seen = set()
    for p in periods:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        uniq_periods.append(p)
    return {"dungeons": dungeons, "periods": uniq_periods}


def _parse_role_rows_from_table(table):
    rows = []
    for tr in table.find_all("tr") or []:
        tds = tr.find_all("td") or []
        if len(tds) < 6:
            continue
        rank_text = (tds[0].get_text() or "").strip()
        if not rank_text.isdigit():
            continue
        rank = int(rank_text)
        diff_raw = (tds[1].get_text() or "").strip()
        diff_raw, diff_value = _parse_diff(diff_raw)
        tier = (tds[2].get_text() or "").strip()
        avg_text = (tds[3].get_text() or "").strip()
        top_text = (tds[4].get_text() or "").strip()
        runs_text = (tds[5].get_text() or "").strip()

        spec_name = ""
        spec_url = ""
        link = tr.find("a", href=re.compile(r"/spec/"))
        if link:
            spec_name = (link.get_text() or "").strip()
            href = (link.get("href") or "").strip()
            if href.startswith("/"):
                spec_url = "https://mythicstats.com" + href
            else:
                spec_url = href
        spec_slug = _extract_slug_from_spec_url(spec_url)

        rows.append(
            {
                "rank": rank,
                "diff_raw": diff_raw,
                "diff_value": diff_value,
                "tier": tier,
                "avg_text": avg_text,
                "avg_value": _parse_suffix_number(avg_text),
                "top_text": top_text,
                "top_value": _parse_suffix_number(top_text),
                "runs_text": runs_text,
                "runs_value": _parse_suffix_number(runs_text),
                "spec_name": spec_name,
                "spec_slug": spec_slug,
                "spec_url": spec_url,
            }
        )
    return rows


def _parse_rankings_from_html(html):
    try:
        from bs4 import BeautifulSoup
    except Exception:
        BeautifulSoup = None

    if not html:
        return {"damage": [], "tank": [], "healer": []}

    if not BeautifulSoup:
        return {"damage": [], "tank": [], "healer": []}

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table") or []
    by_role = {"damage": [], "tank": [], "healer": []}

    for table in tables:
        role = ""
        cur = table
        for _ in range(6):
            cur = cur.find_previous(["h1", "h2", "h3", "h4", "p", "div", "span"])
            if not cur:
                break
            t = (cur.get_text(" ", strip=True) or "").lower()
            if "damage specs" in t:
                role = "damage"
                break
            if "tank specs" in t:
                role = "tank"
                break
            if "healer specs" in t:
                role = "healer"
                break
        if not role:
            continue
        rows = _parse_role_rows_from_table(table)
        if rows:
            by_role[role] = rows

    return by_role


def fetch_mythicstats_dps(*, req=None, season="season-mn-1", dungeon_id=0, period_id=None):
    season = _parse_season(season)
    if season == "season-mn-1":
        season = ""
    q = {}
    if dungeon_id and int(dungeon_id) > 0:
        q["dungeon"] = int(dungeon_id)
    if period_id:
        q["period"] = int(period_id)
    url = "https://mythicstats.com/dps"
    if q:
        url = url + "?" + urlencode(q)

    html = _get_html(req, url)
    meta = _parse_meta_from_html(html)
    periods = meta.get("periods") or []
    if not period_id:
        if periods:
            period_id = periods[0]["id"]
        else:
            period_id = None
    period_label = ""
    if period_id and periods:
        for p in periods:
            if int(p.get("id") or 0) == int(period_id):
                period_label = p.get("label") or ""
                break
    if not period_label and period_id:
        period_label = str(period_id)

    if not season and period_id:
        season_slug, _season_label = fetch_period_season_slug(req=req, period_id=int(period_id))
        if season_slug:
            season = season_slug
    if not season:
        season = "unknown"

    rankings = _parse_rankings_from_html(html)

    return {
        "season": season,
        "dungeon_id": int(dungeon_id or 0),
        "dungeons": meta.get("dungeons") or [],
        "period_id": int(period_id) if period_id else None,
        "period_label": period_label,
        "periods": periods,
        "rankings": rankings,
    }


def upsert_mythicstats_dps_rows(
    *,
    season,
    period_id,
    period_label,
    dungeon_id,
    dungeon_name,
    role,
    rows,
    replace_batch=False,
):
    season = _parse_season(season) or "unknown"
    period_id = int(period_id)
    dungeon_id = int(dungeon_id or 0)
    role = (role or "").strip() or "damage"
    week = _extract_week_from_label(period_label)

    qs = PortalMythicstatsDpsRow.objects.filter(
        season=season,
        period_id=period_id,
        dungeon_id=dungeon_id,
        role=role,
    )
    if replace_batch:
        qs.delete()

    objs = []
    for r in rows or []:
        spec_slug = (r.get("spec_slug") or "").strip()
        if not spec_slug:
            continue
        objs.append(
            PortalMythicstatsDpsRow(
                season=season,
                period_id=period_id,
                period_label=period_label or "",
                week=week,
                dungeon_id=dungeon_id,
                dungeon_name=dungeon_name or "",
                role=role,
                rank=int(r.get("rank") or 0),
                diff_raw=(r.get("diff_raw") or "").strip(),
                diff_value=r.get("diff_value"),
                tier=(r.get("tier") or "").strip(),
                avg_text=(r.get("avg_text") or "").strip(),
                avg_value=r.get("avg_value"),
                top_text=(r.get("top_text") or "").strip(),
                top_value=r.get("top_value"),
                runs_text=(r.get("runs_text") or "").strip(),
                runs_value=r.get("runs_value"),
                spec_name=(r.get("spec_name") or "").strip(),
                spec_slug=spec_slug,
                spec_url=(r.get("spec_url") or "").strip(),
            )
        )

    if objs:
        PortalMythicstatsDpsRow.objects.bulk_create(objs, ignore_conflicts=True)


def upsert_mythicstats_meta_cache(*, season, dungeons, periods):
    season = _parse_season(season) or "unknown"
    payload = {
        "season": season,
        "dungeons": dungeons or [],
        "periods": periods or [],
        "updated_at": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    import json
    PortalCache.objects.update_or_create(
        key=f"mythicstats_dps_meta:{season}",
        defaults={"data": json.dumps(payload, ensure_ascii=False), "status": 0, "error_message": ""},
    )
