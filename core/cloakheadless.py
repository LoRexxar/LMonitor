import glob
import os
import shutil
import time
import traceback

from utils.log import logger

try:
    from django.conf import settings as django_settings
except Exception:
    django_settings = None

try:
    from LMonitor.settings import PROXY_CONFIG
except Exception:
    PROXY_CONFIG = None


def _cleanup_stale_playwright_temp():
    tmp = os.getenv('TEMP') or os.getenv('TMP') or '/tmp'
    if not tmp or not os.path.isdir(tmp):
        return
    prefix = os.path.join(tmp, 'playwright_chromiumdev_profile-')
    for d in glob.glob(prefix + '*'):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


class CloakDriver:
    def __init__(self, is_proxy=False):
        self.is_proxy = bool(is_proxy)
        self.browser = None
        self.context = None
        self.page = None
        # playwright 实例（fallback 模式下需要手动 stop）
        self._pw = None
        try:
            self.init_object(is_proxy)
        except Exception:
            raise
        self.origin_url = ""

    def _get_proxy(self):
        if not self.is_proxy:
            return None
        proxy = str((PROXY_CONFIG or {}).get("http") or (PROXY_CONFIG or {}).get("https") or "").strip()
        return proxy or None

    def init_object(self, is_proxy=False):
        """
        优先使用 cloakbrowser（带 stealth chromium），失败则 fallback 到官方 playwright chromium。

        说明：cloakbrowser 的 stealth chromium 在部分 Windows 环境可能会因为
        安全策略/依赖缺失而无法 spawn（常见报错 BrowserType.launch: spawn UNKNOWN）。
        这时 fallback 仍然可以提供 headless 渲染能力，避免任务直接降级到 requests。
        """
        launch = None
        launch_persistent_context = None
        try:
            from cloakbrowser import launch as _launch, launch_persistent_context as _lpc
            launch = _launch
            launch_persistent_context = _lpc
        except Exception:
            launch = None
            launch_persistent_context = None

        def _proxy_to_playwright(proxy_val):
            if not proxy_val:
                return None
            if isinstance(proxy_val, dict):
                return proxy_val
            # string -> {"server": "..."}
            return {"server": str(proxy_val)}

        def _launch_playwright(user_data_dir, headless, proxy_val, args):
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            pw_proxy = _proxy_to_playwright(proxy_val)
            if user_data_dir:
                self.context = self._pw.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=headless,
                    proxy=pw_proxy,
                    args=args,
                )
                self.page = self.context.new_page()
                self.browser = None
                return
            self.browser = self._pw.chromium.launch(
                headless=headless,
                proxy=pw_proxy,
                args=args,
            )
            self.context = self.browser.new_context()
            self.page = self.context.new_page()

        wcl_cfg = getattr(django_settings, "WCL_FETCH_CONFIG", {}) if django_settings else {}
        wcl_cfg = wcl_cfg or {}
        headless = not bool(wcl_cfg.get("disable_headless", False))

        args = [
            "--no-sandbox",
            "--log-level=3",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,1080",
        ]

        profile_dir = (wcl_cfg.get("chrome_profile_directory") or "").strip()
        if profile_dir:
            args.append(f"--profile-directory={profile_dir}")

        proxy = self._get_proxy() if is_proxy else None

        _cleanup_stale_playwright_temp()

        user_data_dir = (wcl_cfg.get("chrome_user_data_dir") or "").strip()
        last_exc = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    time.sleep(2 * attempt)
                    _cleanup_stale_playwright_temp()
                if launch and launch_persistent_context:
                    if user_data_dir:
                        self.context = launch_persistent_context(user_data_dir, headless=headless, proxy=proxy, args=args)
                        self.page = self.context.new_page()
                        self.browser = None
                        return
                    self.browser = launch(headless=headless, proxy=proxy, args=args)
                    self.context = self.browser.new_context()
                    self.page = self.context.new_page()
                    return

                # cloakbrowser 不可用时直接走 playwright
                _launch_playwright(user_data_dir, headless, proxy, args)
                return
            except Exception as e:
                last_exc = e
                short_msg = str(e).split('\n')[0][:200]
                logger.warning(f"[Cloak Headless] launch attempt {attempt + 1} failed: {short_msg}")
                try:
                    self.close_driver()
                except Exception:
                    pass
                if "asyncio loop" in str(e).lower():
                    break
                # 如果 cloakbrowser 存在但启动失败（如 spawn UNKNOWN），下一次尝试改走 playwright fallback
                if launch and ("spawn unknown" in str(e).lower() or "browsertype.launch" in str(e).lower()):
                    launch = None
                    launch_persistent_context = None
        if last_exc:
            raise last_exc

    def _rebuild(self):
        try:
            self.close_driver()
        except Exception:
            pass
        self.init_object(self.is_proxy)

    def get_resp(self, url, cookies=None, is_origin=0, times=0):
        try:
            req_cfg = getattr(django_settings, "REQUEST_CONFIG", {}) if django_settings else {}
            req_cfg = req_cfg or {}
            max_retries = int(req_cfg.get("chrome_retries", 1))
            if times > max_retries:
                return False

            if not self.page or not self.context:
                return False

            if cookies:
                try:
                    if isinstance(cookies, dict):
                        cookie_list = [{"name": k, "value": str(v), "url": url} for k, v in cookies.items()]
                        self.context.add_cookies(cookie_list)
                    elif isinstance(cookies, str):
                        cookie_list = []
                        for kv in cookies.split(';'):
                            kv = kv.strip()
                            if not kv or '=' not in kv:
                                continue
                            k, v = kv.split('=', 1)
                            k = k.strip()
                            v = v.strip()
                            if not k:
                                continue
                            cookie_list.append({"name": k, "value": v, "url": url})
                        if cookie_list:
                            self.context.add_cookies(cookie_list)
                    else:
                        self.context.add_cookies(cookies)
                except Exception:
                    pass

            self.page.goto(url, wait_until="domcontentloaded")
            try:
                self.page.wait_for_load_state("networkidle")
            except Exception:
                pass
            source = self.page.content()

            if is_origin:
                return self.page
            return source

        except Exception:
            logger.error("[Cloak Headless] {}".format(traceback.format_exc()))
            req_cfg = getattr(django_settings, "REQUEST_CONFIG", {}) if django_settings else {}
            req_cfg = req_cfg or {}
            max_retries = int(req_cfg.get("chrome_retries", 1))
            if times >= max_retries:
                return False
            try:
                self._rebuild()
            except Exception:
                logger.error("[Cloak Headless] {}".format(traceback.format_exc()))
                return False
            return self.get_resp(url, cookies=cookies, is_origin=is_origin, times=times + 1)

    def close_driver(self):
        try:
            if self.page:
                self.page.close()
        except Exception:
            pass
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        try:
            time.sleep(1)
        except Exception:
            pass

    def __del__(self):
        self.close_driver()
