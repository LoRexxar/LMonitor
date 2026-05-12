import html
import json
import os
import re
import sys

import django
import requests


def _build_exists(build, locale="enUS"):
    u = f"https://wago.tools/db2/ChrClasses/csv?build={build}&locale={locale}"
    try:
        r = requests.get(u, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code == 200
    except Exception:
        return False


def _resolve_build(build, locale="enUS"):
    build = (build or "").strip()
    if not build:
        return ""
    if _build_exists(build, locale=locale):
        return build
    m = re.match(r"^(\d+\.\d+\.\d+)\.(\d+)$", build)
    if not m:
        return ""
    prefix = m.group(1)
    base = int(m.group(2))
    for d in range(1, 11):
        cand = f"{prefix}.{base + d}"
        if _build_exists(cand, locale=locale):
            return cand
    for d in range(1, 11):
        cand = f"{prefix}.{base - d}"
        if _build_exists(cand, locale=locale):
            return cand
    return ""


def _iter_build_pages():
    next_url = "https://wago.tools/builds"
    while next_url:
        t = requests.get(next_url, timeout=30).text
        m = re.search(r'data-page="([^"]+)"', t)
        if not m:
            return
        obj = json.loads(html.unescape(m.group(1)))
        props = obj.get("props") or {}
        builds = props.get("builds") or {}
        rows = builds.get("data") if isinstance(builds, dict) else []
        yield rows or []
        next_url = builds.get("next_page_url") if isinstance(builds, dict) else None
        if next_url and next_url.startswith("/"):
            next_url = "https://wago.tools" + next_url


def _find_product_for_build(version):
    version = (version or "").strip()
    if not version:
        return ""
    for rows in _iter_build_pages():
        for r in rows:
            v = (r.get("version") or "").strip()
            if not v:
                patch = str(r.get("patch") or "").strip()
                build = str(r.get("build") or "").strip()
                if patch and build:
                    v = f"{patch}.{build}"
            if v == version:
                return (r.get("product") or "").strip()
    return ""


def _get_prev_build(product, to_build):
    versions = []
    found = False
    for rows in _iter_build_pages():
        for r in rows:
            if (r.get("product") or "").strip() != product:
                continue
            v = (r.get("version") or "").strip()
            if not v:
                patch = str(r.get("patch") or "").strip()
                build = str(r.get("build") or "").strip()
                if patch and build:
                    v = f"{patch}.{build}"
            if v and v not in versions:
                versions.append(v)
        if to_build in versions:
            found = True
        if found:
            idx = versions.index(to_build)
            if idx + 1 < len(versions):
                break
        if len(versions) > 300:
            break
    if to_build not in versions:
        return ""
    idx = versions.index(to_build)
    return versions[idx + 1] if idx + 1 < len(versions) else ""


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: python scripts/generate_wow_skilldiff_for_build.py <product> <to_build> [wowhead_url]")
    product = (sys.argv[1] or "").strip()
    req_to_build = (sys.argv[2] or "").strip()
    wowhead_url = (sys.argv[3] or "").strip() if len(sys.argv) >= 4 else ""
    if not product or not req_to_build:
        raise SystemExit("product/to_build required")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "LMonitor.settings")
    django.setup()

    from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
    from botend.models import WowSkillDiffReport
    from utils.LReq import LReq

    to_build = _resolve_build(req_to_build) or req_to_build
    display_to_build = req_to_build if req_to_build != to_build else ""

    prev_product = _find_product_for_build(to_build) or product
    from_build = _get_prev_build(prev_product, to_build)
    if not from_build:
        raise SystemExit(f"prev build not found for {prev_product} {to_build}")

    mon = WagoSkillDiffMonitor(LReq(is_chrome=False), task=None)
    report = mon._generate_report(
        product,
        from_build,
        to_build,
        display_from_build="",
        display_to_build=display_to_build,
        wowhead_url=wowhead_url,
    )
    if not report:
        raise SystemExit("no report generated")

    row, _ = WowSkillDiffReport.objects.update_or_create(
        branch=product,
        locale=mon.locale,
        to_build=to_build,
        defaults={
            "from_build": from_build,
            "display_from_build": report.get("display_from_build") or "",
            "display_to_build": report.get("display_to_build") or "",
            "content_md": report.get("content_md") or "",
            "content_html_path": report.get("content_html_path") or "",
            "changed_tables_json": report.get("changed_tables_json") or "",
            "spell_count": int(report.get("spell_count") or 0),
            "class_count": int(report.get("class_count") or 0),
        },
    )
    print("report_id", row.id)
    print("branch", product)
    print("from", from_build)
    print("to", to_build)
    print("spell_count", row.spell_count)
    print("class_count", row.class_count)


if __name__ == "__main__":
    main()
