import html
import json
import os
import re
import sys

import django
import requests


def _load_home_props():
    t = requests.get("https://wago.tools/", timeout=30).text
    m = re.search(r'data-page="([^"]+)"', t)
    if not m:
        return {}
    raw = html.unescape(m.group(1))
    obj = json.loads(raw)
    return obj.get("props") or {}


def _extract_versions(props, product="wow", limit=60):
    versions = props.get("versions") or []
    cur = ""
    for v in versions:
        if (v.get("product") or "").strip() == product:
            cur = (v.get("version") or "").strip()
            break

    out = []
    next_url = "https://wago.tools/builds"
    while next_url and len(out) < limit:
        t = requests.get(next_url, timeout=30).text
        m = re.search(r'data-page="([^"]+)"', t)
        if not m:
            break
        obj = json.loads(html.unescape(m.group(1)))
        props2 = obj.get("props") or {}
        builds = props2.get("builds") or {}
        rows = builds.get("data") if isinstance(builds, dict) else []
        for r in rows or []:
            if (r.get("product") or "").strip() != product:
                continue
            v = (r.get("version") or "").strip()
            if not v:
                patch = str(r.get("patch") or "").strip()
                build = str(r.get("build") or "").strip()
                if patch and build:
                    v = f"{patch}.{build}"
            if v and v not in out:
                out.append(v)
            if len(out) >= limit:
                break
        next_url = builds.get("next_page_url") if isinstance(builds, dict) else None
        if next_url and next_url.startswith("/"):
            next_url = "https://wago.tools" + next_url
    if cur and cur not in out:
        out.insert(0, cur)
    return out


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "LMonitor.settings")
    django.setup()

    from botend.models import WowSkillDiffReport
    from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
    from utils.LReq import LReq

    branch = (sys.argv[1] if len(sys.argv) > 1 else "wow").strip() or "wow"
    props = _load_home_props()
    versions = _extract_versions(props, product=branch, limit=50)
    if len(versions) < 2:
        raise SystemExit("not enough versions")

    req = LReq(is_chrome=False)
    mon = WagoSkillDiffMonitor(req, task=None)

    best = None
    best_report = None
    max_pairs = min(22, len(versions) - 1)
    for i in range(max_pairs):
        to_build = versions[i]
        from_build = versions[i + 1]
        report = mon._generate_report(branch, from_build, to_build)
        if not report:
            continue
        spell_count = int(report.get("spell_count") or 0)
        if not best or spell_count > int(best_report.get("spell_count") or 0):
            row, _ = WowSkillDiffReport.objects.update_or_create(
                branch=branch,
                locale=mon.locale,
                to_build=to_build,
                defaults={
                    "from_build": from_build,
                    "content_md": report.get("content_md") or "",
                    "changed_tables_json": report.get("changed_tables_json") or "",
                    "spell_count": spell_count,
                    "class_count": int(report.get("class_count") or 0),
                },
            )
            best = row
            best_report = report
        if best and best.spell_count >= 10:
            break

    if not best:
        raise SystemExit("no report generated")

    print("report_id", best.id)
    print("branch", best.branch)
    print("from", best.from_build)
    print("to", best.to_build)
    print("spell_count", best.spell_count)
    print("class_count", best.class_count)


if __name__ == "__main__":
    main()
