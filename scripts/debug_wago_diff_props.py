import html
import json
import re

import requests


def dump(url):
    t = requests.get(url, timeout=30).text
    m = re.search(r'data-page="([^"]+)"', t)
    print("url", url)
    print("has_data_page", bool(m))
    if not m:
        print(t[:500])
        return
    obj = json.loads(html.unescape(m.group(1)))
    props = obj.get("props") or {}
    print("component", obj.get("component"))
    print("props_keys", list(props.keys())[:80])
    if "entries" in props:
        entries = props.get("entries") or []
        print("entries_type", type(entries).__name__, "len", len(entries) if hasattr(entries, "__len__") else "n/a")
        if isinstance(entries, list) and entries:
            e0 = entries[0]
            if isinstance(e0, dict):
                print("entry0_keys", list(e0.keys())[:60])
            print("entry0", str(e0)[:500])
        if isinstance(entries, dict):
            ks = list(entries.keys())
            print("entries_keys", ks[:40])
            data = entries.get("data") or []
            print("entries_data_len", len(data))
            if data:
                d0 = data[0]
                if isinstance(d0, dict):
                    print("entry0_keys", list(d0.keys())[:60])
                print("entry0", str(d0)[:500])
    if "items" in props:
        items = props.get("items") or []
        print("items_type", type(items).__name__, "len", len(items) if hasattr(items, "__len__") else "n/a")
        if isinstance(items, list) and items:
            i0 = items[0]
            if isinstance(i0, dict):
                print("item0_keys", list(i0.keys())[:60])
            print("item0", str(i0)[:500])
        if isinstance(items, dict):
            ks = list(items.keys())
            print("items_keys", ks[:40])
            data = items.get("data") or []
            print("items_data_len", len(data))
            if data:
                d0 = data[0]
                if isinstance(d0, dict):
                    print("item0_keys", list(d0.keys())[:60])
                print("item0", str(d0)[:500])


def main():
    dump("https://wago.tools/db2/spellname/diff?from=12.0.5.67403&to=12.0.5.67451")
    dump("https://wago.tools/builds-diff?to=12.0.5.67451&from=12.0.5.67403")


if __name__ == "__main__":
    main()
