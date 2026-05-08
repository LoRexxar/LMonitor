#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: chromeheadless.py.py
@time: 2020/3/17 15:17
@desc:
'''

import time

from DrissionPage import ChromiumPage
from DrissionPage import ChromiumOptions
from DrissionPage.common import Settings
from DrissionPage.common import Keys
from DrissionPage.common import Actions
from DrissionPage.common import By
from DrissionPage.errors import ElementNotFoundError

import os
import traceback
import DrissionPage
from urllib.parse import urlparse
from django.conf import settings as django_settings

from LMonitor.settings import CHROME_WEBDRIVER_PATH, PROXY_CONFIG
from utils.base import random_string
from utils.log import logger


class ChromeDriver:
    def __init__(self, is_proxy=False):
        # self.chromedriver_path = CHROME_WEBDRIVER_PATH
        # self.checkos()

        self.is_proxy = bool(is_proxy)
        try:
            self.init_object(is_proxy)
        except Exception:
            logger.error("[Chrome Headless] {}".format(traceback.format_exc()))
            raise

        self.origin_url = ""

    def checkos(self):

        if os.name == 'nt':
            self.chromedriver_path = os.path.join(self.chromedriver_path, "chromedriver_win32.exe")
        elif os.name == 'posix':
            self.chromedriver_path = os.path.join(self.chromedriver_path, "chromedriver_linux64")
        else:
            self.chromedriver_path = os.path.join(self.chromedriver_path, "chromedriver_mac64")

    def init_object(self, is_proxy=False):

        self.chrome_options = ChromiumOptions()
        wcl_cfg = getattr(django_settings, 'WCL_FETCH_CONFIG', {}) or {}

        self.chrome_options.no_imgs(True).mute(True)
        if not bool(wcl_cfg.get('disable_headless', False)):
            self.chrome_options.headless()
        self.chrome_options.set_argument('--no-sandbox')  # 无沙盒模式
        self.chrome_options.set_argument("--log-level=3")
        self.chrome_options.set_argument("--disable-blink-features=AutomationControlled")
        self.chrome_options.set_argument("--window-size=1920,1080")

        user_data_dir = (wcl_cfg.get('chrome_user_data_dir') or '').strip()
        if user_data_dir:
            self.chrome_options.set_argument(f"--user-data-dir={user_data_dir}")
        profile_dir = (wcl_cfg.get('chrome_profile_directory') or '').strip()
        if profile_dir:
            self.chrome_options.set_argument(f"--profile-directory={profile_dir}")

        tmp_dir = (wcl_cfg.get('tmp_dir') or os.getenv('TEMP') or os.getenv('TMP') or '/tmp')
        self.chrome_options.set_tmp_path(tmp_dir)
        
        if is_proxy:
            self.chrome_options.set_proxy('{}'.format(PROXY_CONFIG["http"]))

        self.driver = ChromiumPage(self.chrome_options)

    def _rebuild(self):
        try:
            self.close_driver()
        except Exception:
            pass
        self.init_object(self.is_proxy)

    def get_resp(self, url, cookies=None, is_origin=0, times=0):
        """
        """
        try:
            req_cfg = getattr(django_settings, 'REQUEST_CONFIG', {}) or {}
            max_retries = int(req_cfg.get('chrome_retries', 1))
            if times > max_retries:
                return False

            self.driver.get(url)

            if cookies:
                self.driver.set.cookies(cookies)
                self.driver.get(url)

            self.driver.wait.load_start()
            source = self.driver.html

            if is_origin:
                return self.driver

            return source

        except DrissionPage.errors.PageDisconnectedError:
            logger.warning("[ChromeHeadless] PageDisconnectedError..{}".format(url))
            if times >= int((getattr(django_settings, 'REQUEST_CONFIG', {}) or {}).get('chrome_retries', 1)):
                return False
            try:
                self._rebuild()
            except Exception:
                logger.error("[Chrome Headless] {}".format(traceback.format_exc()))
                return False
            return self.get_resp(url, cookies, is_origin=is_origin, times=times + 1)

        except DrissionPage.errors.ContextLostError:
            logger.warning("[ChromeHeadless] page get error..{}".format(url))

            logger.warning("[ChromeHeadless]retry once..{}".format(url))
            try:
                self._rebuild()
            except Exception:
                pass
            return self.get_resp(url, cookies, is_origin=is_origin, times=times + 1)

        except ElementNotFoundError:
            logger.warning("[ChromeHeadless] Not found target element..{}".format(url))

            logger.warning("[ChromeHeadless]retry once..{}".format(url))
            return self.get_resp(url, cookies, is_origin=is_origin, times=times + 1)

        except:
            logger.error("[Chrome Headless] {}".format(traceback.format_exc()))
            return False

    def close_driver(self):
        try:
            self.driver.quit()
        except Exception:
            pass
        try:
            time.sleep(1)
        except Exception:
            pass

    def __del__(self):
        self.close_driver()
