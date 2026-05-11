import re
import html
import json

import requests


def main():
    url = "https://wago.tools/db2/spellmisc?build=12.0.5.67088&locale=enUS&filter%5BID%5D=exact%3A268"
    t = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    print("len", len(t))
    print("has_data_page", "data-page" in t)
    m1 = re.search(r'data-page="([^"]+)"', t)
    m2 = re.search(r"data-page='([^']+)'", t)
    print("match_dq", bool(m1))
    print("match_sq", bool(m2))
    idx = t.find("data-page")
    if idx >= 0:
        print(t[idx:idx + 200])
    m1 = re.search(r'data-page="([^"]+)"', t)
    obj = json.loads(html.unescape(m1.group(1))) if m1 else {}
    props = obj.get("props") or {}
    print("component", obj.get("component"))
    print("props_keys", list(props.keys())[:40])
    entries = props.get("entries")
    print("entries_type", type(entries).__name__)
    if isinstance(entries, dict):
        data = entries.get("data") or []
        print("entries_data_len", len(data))
        if data:
            d0 = data[0]
            print("first_keys", list(d0.keys())[:40])
            print("first_id", d0.get("ID"), "spell", d0.get("Spell") or d0.get("SpellID"))


if __name__ == "__main__":
    main()
