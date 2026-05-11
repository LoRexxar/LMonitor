import html
import json
import re
import sys

import requests


def iter_build_pages():
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
        yield builds, rows or []
        next_url = builds.get("next_page_url") if isinstance(builds, dict) else None
        if next_url and next_url.startswith("/"):
            next_url = "https://wago.tools" + next_url


def main():
    product = (sys.argv[1] if len(sys.argv) > 1 else "wowt").strip()
    prefix = (sys.argv[2] if len(sys.argv) > 2 else "12.0.5.665").strip()
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 200

    seen = set()
    out = []
    pages = 0
    for _, rows in iter_build_pages():
        pages += 1
        for r in rows:
            if (r.get("product") or "").strip() != product:
                continue
            v = str(r.get("version") or "").strip()
            if not v.startswith(prefix):
                continue
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    out_sorted = sorted(out)
    print("product", product)
    print("prefix", prefix)
    print("pages_scanned", pages)
    print("count", len(out_sorted))
    for v in out_sorted:
        print(v)


if __name__ == "__main__":
    main()

