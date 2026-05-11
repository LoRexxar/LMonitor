import html
import json
import re

import requests


def main():
    prod = "wowt"
    target = "12.0.5.66527"
    next_url = "https://wago.tools/builds"
    seen = set()
    versions = []
    pages = 0
    while next_url and pages < 20:
        t = requests.get(next_url, timeout=30).text
        m = re.search(r'data-page="([^"]+)"', t)
        if not m:
            break
        obj = json.loads(html.unescape(m.group(1)))
        props = obj.get("props") or {}
        builds = props.get("builds") or {}
        rows = builds.get("data") if isinstance(builds, dict) else []
        for r in rows or []:
            if (r.get("product") or "").strip() != prod:
                continue
            v = (r.get("version") or "").strip()
            if v and v not in seen:
                seen.add(v)
                versions.append(v)
        pages += 1
        next_url = builds.get("next_page_url") if isinstance(builds, dict) else None
        if next_url and next_url.startswith("/"):
            next_url = "https://wago.tools" + next_url
    print("pages", pages)
    print("count", len(versions))
    print("has_target", target in versions)
    if target in versions:
        i = versions.index(target)
        print("idx", i, "prev", versions[i + 1] if i + 1 < len(versions) else None)
    print("head", versions[:40])


if __name__ == "__main__":
    main()

