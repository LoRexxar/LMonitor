import html
import json
import os
import re
import sys

import django
import requests


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


def _get_versions(product, limit=30):
    out = []
    seen = set()
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
            if v and v not in seen:
                seen.add(v)
                out.append(v)
            if len(out) >= limit:
                return out
    return out


def main():
    product = (sys.argv[1] if len(sys.argv) > 1 else "wowt").strip() or "wowt"
    pair_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "LMonitor.settings")
    django.setup()

    from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
    from utils.LReq import LReq

    versions = _get_versions(product, limit=max(25, pair_limit + 1))
    if len(versions) < 2:
        raise SystemExit("not enough versions")

    mon = WagoSkillDiffMonitor(LReq(is_chrome=False), task=None)

    tested = 0
    for i in range(min(pair_limit, len(versions) - 1)):
        to_build = versions[i]
        from_build = versions[i + 1]
        changed = mon._fetch_changed_db2_tables(from_build, to_build)
        rel = [t for t in changed if (t or "").lower() in mon.core_tables]
        if not rel:
            continue

        spec_to_class = mon._load_chr_specialization_to_class(to_build)
        spell_to_specs = mon._load_specialization_spells(to_build)
        if not spec_to_class or not spell_to_specs:
            continue

        count = 0
        for t in rel:
            rows = mon._fetch_db2_diff_rows(t, from_build, to_build)
            for row in rows or []:
                sid = mon._extract_spell_id((t or "").lower(), row)
                if not sid:
                    continue
                if mon._spell_has_class(sid, spell_to_specs, spec_to_class, to_build):
                    count += 1
            if count >= 10:
                break
        tested += 1
        if count > 0:
            print("product", product)
            print("from", from_build)
            print("to", to_build)
            print("match_rows", count)
            return
    print("no_match_in_pairs", tested)


if __name__ == "__main__":
    main()

