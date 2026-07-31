from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings


class CrawlIconsCommandTests(SimpleTestCase):
    @override_settings()
    @patch('botend.management.commands.crawl_icons.requests.get')
    def test_explicit_icon_bypasses_business_data_scan(self, get):
        response = Mock(status_code=200, content=b'jpeg-data' * 20)
        get.return_value = response

        with TemporaryDirectory() as static_root:
            with override_settings(BASE_DIR=static_root):
                output = StringIO()
                call_command(
                    'crawl_icons', '--size', 'small', '--icon', 'inv_test_icon',
                    stdout=output,
                )

        self.assertIn('定向下载 1 个图标，不扫描业务数据', output.getvalue())
        get.assert_called_once_with(
            'https://wow.zamimg.com/images/wow/icons/small/inv_test_icon.jpg',
            timeout=10,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            },
        )