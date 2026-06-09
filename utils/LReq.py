#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: request.py
@time: 2020/3/13 15:48
@desc:
'''

import json
import time
import requests
import random
import traceback
import socket
import urllib3
from urllib.parse import urlparse

from utils.log import logger
try:
    from django.conf import settings as django_settings
except Exception:
    django_settings = None
try:
    from core.chromeheadless import ChromeDriver
except Exception:
    ChromeDriver = None
try:
    from core.cloakheadless import CloakDriver
except Exception:
    CloakDriver = None


class LReq:
    """
    请求类
    """
    def __init__(self, is_chrome=False, is_cloak=False):

        # NOTE:
        # UA 中曾包含无效/过旧的字符串（如 Safari/538），会导致部分站点（例如 wowhead）
        # 直接返回 403。这里改为更现代且格式正确的 UA 列表。
        self.ua = [
            # Chrome (Windows)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            # Edge (Windows)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
            # Firefox (Windows)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        ]
        # wowhead 对部分 Chrome/Edge UA 会返回 403，这里固定使用 Firefox UA
        self.ua_wowhead = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

        self.s = requests.Session()
        # 允许 requests 使用系统/环境代理（HTTP_PROXY/HTTPS_PROXY 等），
        # 否则在部分网络环境下（例如 wowhead）会出现 403/握手失败导致抓不到正文。
        self.s.trust_env = True
        self.is_chrome = bool(is_chrome and ChromeDriver)
        self.is_cloak = bool(is_cloak and CloakDriver)
        self.csp = False
        self.clp = False
        self.cl = None
        self.current_task = None
        self._cfg = self._get_cfg()
        self._apply_session_proxy()

        if self.is_chrome:
            try:
                self.cs = ChromeDriver()
            except Exception:
                self.is_chrome = False
                self.cs = None
                logger.warning("[LReq] ChromeDriver init failed, fallback to requests mode")
        elif is_chrome and not ChromeDriver:
            logger.warning("[LReq] ChromeDriver not available, fallback to requests mode")

        if self.is_cloak:
            try:
                self.cl = CloakDriver()
            except Exception:
                self.is_cloak = False
                self.cl = None
                logger.warning("[LReq] CloakDriver init failed, fallback to requests mode")
        elif is_cloak and not CloakDriver:
            logger.warning("[LReq] CloakDriver not available, fallback to requests mode")

    def _get_cfg(self):
        if django_settings:
            return getattr(django_settings, 'REQUEST_CONFIG', {}) or {}
        return {}

    def _apply_session_proxy(self):
        return

    def _get_global_proxies(self):
        proxies = self._cfg.get('proxies')
        if not proxies and django_settings:
            proxies = getattr(django_settings, 'PROXY_CONFIG', None)
        return proxies if isinstance(proxies, dict) else {}

    def _set_requests_proxy_enabled(self, enabled):
        if enabled:
            proxies = self._get_global_proxies()
            values = [str(v or '').strip().lower() for v in proxies.values()] if isinstance(proxies, dict) else []
            uses_socks = any(v.startswith('socks') for v in values if v)
            if uses_socks:
                try:
                    import socks  # noqa: F401
                except Exception:
                    logger.warning("[LReq] socks proxy enabled but PySocks not installed, skip proxy")
                    self.s.proxies.clear()
                    return
            self.s.proxies.clear()
            self.s.proxies.update(proxies)
            return
        self.s.proxies.clear()

    def set_current_task(self, task):
        self.current_task = task
        task_enabled = bool(getattr(task, 'proxy_enabled', False)) if task else False
        self._set_requests_proxy_enabled(task_enabled)

    def _is_task_proxy_enabled(self):
        task = getattr(self, 'current_task', None)
        if not task:
            return False
        return bool(getattr(task, 'proxy_enabled', False))

    def _ensure_proxy_chrome(self):
        if not self.csp:
            self.csp = ChromeDriver(is_proxy=True)

    def _ensure_proxy_cloak(self):
        if not self.clp:
            self.clp = CloakDriver(is_proxy=True)

    @staticmethod
    def get_timeout():
        return random.randint(1, 5) * 0.5

    def _normalize_timeout_value(self, timeout_value):
        """
        兼容 requests 的 timeout 写法：
        - 3 / "3" -> 3.0
        - (3, 10) / [3, 10] -> (3.0, 10.0)
        同时对异常值做兜底，避免因为配置格式问题直接抛 TypeError。
        """
        try:
            if isinstance(timeout_value, (list, tuple)):
                vals = list(timeout_value)
                if not vals:
                    return 3.0
                if len(vals) == 1:
                    return float(vals[0] or 0)
                return (float(vals[0] or 0), float(vals[1] or 0))
            return float(timeout_value or 0)
        except Exception:
            return 3.0

    def _get_timeout_for_url(self, url, default_timeout):
        """
        某些站点（如 wowhead）TLS 握手/响应更慢，使用过低 timeout 会导致频繁超时，
        从而出现“监控没抓到内容”的假象。这里提供域名级别的 timeout 覆盖。
        """
        try:
            u = str(url or "")
            host = (urlparse(u).netloc or "").lower()
        except Exception:
            host = ""

        overrides = self._cfg.get("timeout_overrides") if isinstance(self._cfg, dict) else None
        if isinstance(overrides, dict) and host:
            # 支持精确 host 或后缀匹配（如 ".wowhead.com"）
            for k, v in overrides.items():
                try:
                    k2 = str(k or "").strip().lower()
                    if not k2:
                        continue
                    if host == k2 or host.endswith(k2.lstrip(".")):
                        return self._normalize_timeout_value(v)
                except Exception:
                    continue

        normalized = self._normalize_timeout_value(default_timeout)

        # 内置兜底：wowhead/暴雪论坛等站点用更大 timeout
        if host.endswith("wowhead.com") or host.endswith("forums.blizzard.com") or host.endswith("us.forums.blizzard.com"):
            if isinstance(normalized, tuple):
                connect_t = max(float(normalized[0] or 0), 20.0)
                read_t = max(float(normalized[1] or 0), 20.0)
                return (connect_t, read_t)
            return max(float(normalized or 0), 20.0)
        return normalized

    def get_header(self, url="", cookies="", ext=None):
        ext = ext or {}
        cookies = cookies if cookies else ""
        if isinstance(cookies, (bytes, bytearray)):
            try:
                cookies = cookies.decode('utf-8', 'ignore')
            except Exception:
                cookies = str(cookies)
        cookies = str(cookies)
        if cookies:
            cookies = cookies.replace('\r', ';').replace('\n', ';')
            cookies = '; '.join([p.strip() for p in cookies.split(';') if p.strip()])
        ua = random.choice(self.ua)
        try:
            u = str(url or "").lower()
            if "wowhead.com" in u:
                ua = getattr(self, "ua_wowhead", ua) or ua
        except Exception:
            pass

        header = {
            "User-Agent": ua,
            "Referer": url,
            "Cookie": cookies,
            # 一些站点（如 wowhead）对缺少基础 Accept 头会更严格，补齐常见浏览器默认值
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        header.update(ext)
        for k, v in list(header.items()):
            if v is None:
                header.pop(k, None)
                continue
            if isinstance(v, (bytes, bytearray)):
                try:
                    v = v.decode('utf-8', 'ignore')
                except Exception:
                    v = str(v)
            v = str(v).replace('\r', ' ').replace('\n', ' ').strip()
            header[k] = v
        if 'Cookie' in header:
            c = header['Cookie'].replace('\r', ';').replace('\n', ';')
            header['Cookie'] = '; '.join([p.strip() for p in c.split(';') if p.strip()])
        return header

    def check_url(self, url):

        if url == "javascript:void(0);":
            return None

        if not urlparse(url).scheme:

            if url.startswith('//'):
                url = 'http:' + url
                return url

            if url.startswith('/') or url.startswith('.'):
                return url

            if not urlparse(url).netloc:
                return url

            url = 'http://' + url

        return url

    def _sleep_backoff(self, attempt):
        base = float(self._cfg.get('backoff_base', 0.5))
        max_s = float(self._cfg.get('backoff_max', 8))
        sleep_s = min(max_s, base * (2 ** max(attempt, 0)))
        sleep_s = sleep_s + (random.random() * 0.2)
        time.sleep(sleep_s)

    def reset_chrome(self):
        if not self.is_chrome:
            return False
        try:
            if getattr(self, 'cs', None):
                self.cs.close_driver()
        except Exception:
            pass
        try:
            self.cs = ChromeDriver()
            return True
        except Exception:
            self.is_chrome = False
            self.cs = None
            return False

    def reset_cloak(self):
        if not self.is_cloak:
            return False
        if CloakDriver and getattr(CloakDriver, '_init_failed', False):
            self.is_cloak = False
            self.cl = None
            return False
        try:
            if getattr(self, 'cl', None):
                self.cl.close_driver()
        except Exception:
            pass
        try:
            if getattr(self, 'clp', None):
                self.clp.close_driver()
        except Exception:
            pass
        self.clp = False
        try:
            self.cl = CloakDriver()
            return True
        except Exception:
            self.is_cloak = False
            self.cl = None
            return False

    def get(self, url, type='Resp', times=0, *args, **kwargs):
        max_retries = int(self._cfg.get('retries', 1))
        attempt = int(times or 0)

        while True:
            try:
                method = getattr(self, 'get' + type)
                return method(url, *args, **kwargs)

            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout, socket.timeout):
                logger.warning("[LReq] Request {} timeout...".format(url))

            except (urllib3.exceptions.NewConnectionError, requests.exceptions.ConnectionError, urllib3.exceptions.MaxRetryError):
                logger.warning("[LReq] Request {} error...".format(url))
                if self.is_chrome:
                    self.reset_chrome()
                if self.is_cloak:
                    self.reset_cloak()

            except requests.exceptions.ChunkedEncodingError:
                logger.warning("[LReq] Request {} chunked encoding error...".format(url))

            except Exception:
                logger.warning('[LReq] something error, {}'.format(traceback.format_exc()))
                return False

            if attempt >= max_retries:
                return False

            attempt += 1
            self._sleep_backoff(attempt)

    def post(self, url, type='Resp', times=0, *args, **kwargs):
        max_retries = int(self._cfg.get('retries', 1))
        attempt = int(times or 0)

        while True:
            try:
                method = getattr(self, 'post' + type)
                return method(url, *args, **kwargs)

            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout, socket.timeout):
                logger.warning("[LReq] Request {} timeout...".format(url))

            except (urllib3.exceptions.NewConnectionError, requests.exceptions.ConnectionError, urllib3.exceptions.MaxRetryError):
                logger.warning("[LReq] Request {} error...".format(url))
                if self.is_chrome:
                    self.reset_chrome()
                if self.is_cloak:
                    self.reset_cloak()

            except requests.exceptions.ChunkedEncodingError:
                logger.warning("[LReq] Request {} chunked encoding error...".format(url))

            except Exception:
                logger.warning('[LReq] something error, {}'.format(traceback.format_exc()))
                return False

            if attempt >= max_retries:
                return False

            attempt += 1
            self._sleep_backoff(attempt)

    def getResp(self, url, cookies):
        url = self.check_url(url)
        logger.info("[LReq] New request {}".format(url))
        cookies = cookies if cookies else ""
        timeout = self._get_timeout_for_url(url, self._cfg.get('timeout', 3))
        r = self.s.get(url, headers=self.get_header(url, cookies), timeout=timeout)
        if getattr(r, 'status_code', 200) >= 400:
            logger.warning("[LReq] Request {} bad status: {}".format(url, r.status_code))

        return r.content

    def getResponse(self, url, cookies, headers=None):
        url = self.check_url(url)
        logger.info("[LReq] New request {}".format(url))
        cookies = cookies if cookies else ""
        timeout = self._get_timeout_for_url(url, self._cfg.get('timeout', 3))
        r = self.s.get(url, headers=self.get_header(url, cookies, headers), timeout=timeout)
        if getattr(r, 'status_code', 200) >= 400:
            logger.warning("[LReq] Request {} bad status: {}".format(url, r.status_code))
        return r

    def getRespByChrome(self, url, cookies, is_origin=0, is_proxy=None):
        url = self.check_url(url)
        logger.info("[LReq] New request {}".format(url))
        cookies = cookies if cookies else ""

        if not self.is_chrome:
            if is_origin:
                return False
            return self.getResp(url, cookies)

        use_proxy = bool(is_proxy) if is_proxy is not None else self._is_task_proxy_enabled()

        if use_proxy:
            self._ensure_proxy_chrome()
            resp = self.csp.get_resp(url, cookies, is_origin=is_origin)
            if resp is False:
                try:
                    self.csp.close_driver()
                except Exception:
                    pass
                self.csp = ChromeDriver(is_proxy=True)
                return self.csp.get_resp(url, cookies, is_origin=is_origin)
            return resp

        resp = self.cs.get_resp(url, cookies, is_origin=is_origin)
        if resp is False:
            if self.reset_chrome():
                return self.cs.get_resp(url, cookies, is_origin=is_origin)
        return resp

    def getRespByCloak(self, url, cookies, is_origin=0, is_proxy=None):
        url = self.check_url(url)
        logger.info("[LReq] New request {}".format(url))
        cookies = cookies if cookies else ""

        if not self.is_cloak:
            if is_origin:
                return False
            return self.getResp(url, cookies)

        use_proxy = bool(is_proxy) if is_proxy is not None else self._is_task_proxy_enabled()

        if use_proxy:
            self._ensure_proxy_cloak()
            resp = self.clp.get_resp(url, cookies, is_origin=is_origin)
            if resp is False:
                try:
                    self.clp.close_driver()
                except Exception:
                    pass
                self.clp = CloakDriver(is_proxy=True)
                return self.clp.get_resp(url, cookies, is_origin=is_origin)
            return resp

        resp = self.cl.get_resp(url, cookies, is_origin=is_origin)
        if resp is False:
            if self.reset_cloak():
                return self.cl.get_resp(url, cookies, is_origin=is_origin)
        return resp

    def postResp(self, url, data, cookies, headers=None):
        headers = headers or {}
        url = self.check_url(url)
        logger.info("[LReq] New request {}".format(url))
        cookies = cookies if cookies else ""
        timeout = self._cfg.get('timeout', 3)
        r = self.s.post(url, data=data, headers=self.get_header(url, cookies, headers), timeout=timeout)
        if getattr(r, 'status_code', 200) >= 400:
            logger.warning("[LReq] Request {} bad status: {}".format(url, r.status_code))

        return r.content

    def postJsonResp(self, url, data, cookies, headers=None):
        headers = headers or {}
        url = self.check_url(url)
        logger.info("[LReq] New request {}".format(url))
        cookies = cookies if cookies else ""

        header = self.get_header(url, cookies, headers)
        header['Content-Type'] = 'application/json'
        timeout = self._cfg.get('timeout', 3)
        r = self.s.post(url, data=json.dumps(data), headers=header, timeout=timeout)
        if getattr(r, 'status_code', 200) >= 400:
            logger.warning("[LReq] Request {} bad status: {}".format(url, r.status_code))

        return r.content

    def close_driver(self):
        if getattr(self, "cs", None):
            self.cs.close_driver()
        if getattr(self, "csp", None):
            self.csp.close_driver()
        if getattr(self, "cl", None):
            self.cl.close_driver()
        if getattr(self, "clp", None):
            self.clp.close_driver()


if __name__ == "__main__":
    Req = LReq(is_chrome=True)

    # print(Req.getResp("https://lorexxar.cn"))
    print(Req.getResp("https://cdn.jsdelivr.net/npm/jquery@3.3.1/dist/jquery.min.js"))
