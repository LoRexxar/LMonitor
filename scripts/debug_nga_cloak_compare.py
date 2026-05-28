import traceback

from utils.LReq import LReq


def _normalize_text(t):
    if t is False or t is None:
        return ""
    if isinstance(t, (bytes, bytearray)):
        try:
            return t.decode("utf-8", "ignore")
        except Exception:
            return str(t)
    return str(t)


def _classify(html):
    text = _normalize_text(html)
    low = text.lower()
    has_topicrows = 'id="topicrows"' in low or "id='topicrows'" in low
    challenge = any(k in low for k in ["turnstile", "cf-", "challenge", "/cdn-cgi/"])
    return {"len": len(text), "topicrows": has_topicrows, "challenge": challenge}


def main():
    urls = [
        "https://nga.178.com/thread.php?fid=310&ff=7",
        "https://nga.178.com/thread.php?fid=7",
    ]
    cookies = ""
    req = None
    try:
        req = LReq(is_chrome=True, is_cloak=True)
        for url in urls:
            print("url", url)
            for t in ("Resp", "RespByChrome", "RespByCloak"):
                try:
                    resp = req.get(url, t, 0, cookies)
                    r = _classify(resp)
                    print(t, r)
                except Exception:
                    print(t, "error", traceback.format_exc())
            print("")
    except Exception:
        print("error", traceback.format_exc())
    finally:
        if req:
            try:
                req.close_driver()
            except Exception:
                pass


if __name__ == "__main__":
    main()
