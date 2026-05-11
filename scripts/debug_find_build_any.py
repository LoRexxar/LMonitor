import html
import json
import re

import requests


def main():
    target = "12.0.5.66527"
    next_url = "https://wago.tools/builds"
    pages = 0
    hits = []
    while next_url and pages < 60:
        t = requests.get(next_url, timeout=30).text
        m = re.search(r'data-page="([^"]+)"', t)
        if not m:
            break
        obj = json.loads(html.unescape(m.group(1)))
        props = obj.get("props") or {}
        builds = props.get("builds") or {}
        rows = builds.get("data") if isinstance(builds, dict) else []
        for r in rows or []:
            v = (r.get("version") or "").strip()
            if v == target:
                hits.append({k: r.get(k) for k in ("product", "branch", "version", "patch", "build")})
        pages += 1
        next_url = builds.get("next_page_url") if isinstance(builds, dict) else None
        if next_url and next_url.startswith("/"):
            next_url = "https://wago.tools" + next_url
    print("pages", pages)
    print("hits", hits)


if __name__ == "__main__":
    main()

