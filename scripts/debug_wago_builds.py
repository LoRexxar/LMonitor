import html
import json
import re

import requests


def main():
    t = requests.get("https://wago.tools/builds", timeout=30).text
    m = re.search(r'data-page="([^"]+)"', t)
    print("has_data_page", bool(m))
    if not m:
        print(t[:800])
        return
    obj = json.loads(html.unescape(m.group(1)))
    props = obj.get("props") or {}
    print("component", obj.get("component"))
    print("props_keys", list(props.keys())[:60])
    builds = props.get("builds") or []
    print("builds_type", type(builds).__name__)
    if isinstance(builds, dict):
        print("builds_keys", list(builds.keys())[:40])
        builds_list = builds.get("data") or builds.get("items") or []
    else:
        builds_list = builds
    print("builds_list_len", len(builds_list))
    if builds_list and isinstance(builds_list[0], dict):
        print("build_item_keys", list(builds_list[0].keys())[:40])
        for i, b in enumerate(builds_list[:3]):
            out = {}
            for k in ("branch", "patch", "build", "version"):
                if k in b:
                    out[k] = b.get(k)
            print("row", i, out)


if __name__ == "__main__":
    main()
