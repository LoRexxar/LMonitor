import html
import json
import re

import requests


def main():
    t = requests.get("https://wago.tools/", timeout=30).text
    m = re.search(r'data-page="([^"]+)"', t)
    if not m:
        print("no data-page")
        return
    obj = json.loads(html.unescape(m.group(1)))
    props = obj.get("props") or {}
    vers = props.get("versions") or []
    print("products", [v.get("product") for v in vers])
    print("rows", [{k: v.get(k) for k in ("product", "branch", "version")} for v in vers])


if __name__ == "__main__":
    main()

