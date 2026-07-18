from unittest import mock

from django.test import SimpleTestCase

from core.chromeheadless import ChromeDriver


class ChromeDriverLifecycleTests(SimpleTestCase):
    @mock.patch("core.chromeheadless.ChromiumPage")
    @mock.patch("core.chromeheadless.ChromiumOptions")
    def test_each_driver_uses_an_automatic_debugging_port(self, options_cls, page_cls):
        options = options_cls.return_value
        for method_name in (
            "no_imgs",
            "mute",
            "headless",
            "set_argument",
            "set_tmp_path",
            "auto_port",
        ):
            getattr(options, method_name).return_value = options

        ChromeDriver()

        options.auto_port.assert_called_once_with()
        page_cls.assert_called_once_with(options)
