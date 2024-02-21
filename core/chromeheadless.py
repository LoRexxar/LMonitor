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
from urllib.parse import urlparse

from LMonitor.settings import CHROME_WEBDRIVER_PATH
from utils.base import random_string
from utils.log import logger


class ChromeDriver:
    def __init__(self):
        # self.chromedriver_path = CHROME_WEBDRIVER_PATH
        # self.checkos()

        try:
            self.init_object()

        except:
            logger.error("[Chrome Headless] {}".format(traceback.format_exc()))
            exit(0)

        self.origin_url = ""

    def checkos(self):

        if os.name == 'nt':
            self.chromedriver_path = os.path.join(self.chromedriver_path, "chromedriver_win32.exe")
        elif os.name == 'posix':
            self.chromedriver_path = os.path.join(self.chromedriver_path, "chromedriver_linux64")
        else:
            self.chromedriver_path = os.path.join(self.chromedriver_path, "chromedriver_mac64")

    def init_object(self):

        self.chrome_options = ChromiumOptions()
        self.chrome_options.no_imgs(True).mute(True)
        self.chrome_options.headless()  # 无头模式
        self.chrome_options.set_argument('--no-sandbox')  # 无沙盒模式
        self.chrome_options.set_argument("--log-level=3")
        self.chrome_options.set_tmp_path("/tmp")

        self.driver = ChromiumPage(self.chrome_options)

    def get_resp(self, url, cookies=None, is_origin=0, times=0):
        """
        """
        try:
            self.driver.get(url)

            if cookies:
                self.driver.set.cookies(cookies)
                self.driver.get(url)

            source = self.driver.html

            if is_origin:
                return self.driver

            return source

        except ElementNotFoundError:
            logger.warning("[ChromeHeadless] Not found target element..{}".format(url))

            logger.warning("[ChromeHeadless]retry once..{}".format(url))
            self.get_resp(url, cookies, is_origin=is_origin, times=times + 1)
            return False

        except:
            logger.error("[Chrome Headless] {}".format(traceback.format_exc()))
            return False

    def close_driver(self):
        self.driver.quit()
        # self.driver.close()
        time.sleep(1)

    def __del__(self):
        self.close_driver()
