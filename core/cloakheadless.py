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


class CloakDriver:
    def __init__(self, is_proxy=False):
        self.is_proxy = bool(is_proxy)
        self.browser = None
        self.context = None
        self.page = None
        try:
            self.init_object(is_proxy)
        except Exception:
            logger.error("[Cloak Headless] {}".format(traceback.format_exc()))
            raise
        self.origin_url = ""

    def _get_proxy(self):
        if not self.is_proxy:
            return None
        proxy = str((PROXY_CONFIG or {}).get("http") or (PROXY_CONFIG or {}).get("https") or "").strip()
        return proxy or None

    def init_object(self, is_proxy=False):
        from cloakbrowser import launch, launch_persistent_context

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

        user_data_dir = (wcl_cfg.get("chrome_user_data_dir") or "").strip()
        if user_data_dir:
            self.context = launch_persistent_context(user_data_dir, headless=headless, proxy=proxy, args=args)
            self.page = self.context.new_page()
            self.browser = None
            return

        self.browser = launch(headless=headless, proxy=proxy, args=args)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

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
            time.sleep(1)
        except Exception:
            pass

    def __del__(self):
        self.close_driver()
