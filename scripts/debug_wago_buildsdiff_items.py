import html
import json
import re
import sys

import requests


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://wago.tools/builds-diff?to=12.0.5.66529&from=12.0.5.66407"
    t = requests.get(url, timeout=30).text
    m = re.search(r'data-page="([^"]+)"', t)
    print("has_data_page", bool(m))
    obj = json.loads(html.unescape(m.group(1))) if m else {}
    props = obj.get("props") or {}
    items = props.get("items") or {}
    data = items.get("data") if isinstance(items, dict) else (items if isinstance(items, list) else [])
    print("items_type", type(items).__name__)
    print("data_len", len(data) if isinstance(data, list) else "n/a")
    if isinstance(data, list) and data:
        types = sorted(set([(d.get("Type") or "") for d in data if isinstance(d, dict)]))
        print("types", types[:20])
        print("sample0", data[0])


if __name__ == "__main__":
    main()

