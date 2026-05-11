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


def _get_current_retail_build(props):
    versions = props.get("versions") or []
    for v in versions:
        if (v.get("product") or "") == "wow":
            return (v.get("version") or "").strip()
    return ""


def _load_builds_rows():
    t = requests.get("https://wago.tools/builds", timeout=30).text
    m = re.search(r'data-page="([^"]+)"', t)
    if not m:
        return []
    raw = html.unescape(m.group(1))
    obj = json.loads(raw)
    props = obj.get("props") or {}
    builds = props.get("builds") or {}
    if isinstance(builds, dict):
        data = builds.get("data") or []
        if isinstance(data, list):
            return data
    return []


def _row_version(row):
    if not isinstance(row, dict):
        return ""
    if row.get("version"):
        return str(row.get("version") or "").strip()
    patch = str(row.get("patch") or "").strip()
    build = str(row.get("build") or "").strip()
    if patch and build:
        return f"{patch}.{build}"
    return ""


def _get_recent_builds(product, limit=20):
    rows = _load_builds_rows()
    latest = []
    for r in rows:
        if (r.get("product") or "").strip() != product:
            continue
        v = _row_version(r)
        if v:
            latest.append(v)
        if len(latest) >= limit:
            break
    return latest


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "LMonitor.settings")
    django.setup()

    from botend.models import WowSkillDiffReport
    from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
    from utils.LReq import LReq

    props = _load_home_props()
    cur_build = _get_current_retail_build(props)
    if not cur_build:
        raise SystemExit("no current retail build")
    branch = "wow"
    builds = _get_recent_builds(branch, limit=40)
    versions = []
    for b in builds:
        if b and b not in versions:
            versions.append(b)
    if len(versions) < 2:
        raise SystemExit("not enough builds")

    created = None
    req = LReq(is_chrome=False)
    mon = WagoSkillDiffMonitor(req, task=None)

    for i in range(len(versions) - 1):
        to_build = versions[i]
        from_build = versions[i + 1]
        report = mon._generate_report(branch, from_build, to_build)
        if not report:
            continue
        row, _ = WowSkillDiffReport.objects.update_or_create(
            branch=branch,
            locale=mon.locale,
            to_build=to_build,
            defaults={
                'from_build': from_build,
                'content_md': report.get('content_md') or '',
                'changed_tables_json': report.get('changed_tables_json') or '',
                'spell_count': int(report.get('spell_count') or 0),
                'class_count': int(report.get('class_count') or 0),
            }
        )
        created = row
        break

    if not created:
        raise SystemExit("no report created from recent builds")

    print("report_id", created.id)
    print("branch", created.branch)
    print("from", created.from_build)
    print("to", created.to_build)
    print("spell_count", created.spell_count)


if __name__ == "__main__":
    main()
