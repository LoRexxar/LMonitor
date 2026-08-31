"""职业配装器图标的有界并发下载与 OSS 直传服务。"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import quote

import requests
from django.conf import settings

from botend.interface.ossupload import ossUploadBytes
from botend.services.article_image_service import _get_configured_proxies


class GearBuilderIconSyncError(RuntimeError):
    pass


def normalize_icon_name(value):
    value = str(value or '').strip().split('?', 1)[0].rsplit('/', 1)[-1]
    for extension in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        if value.lower().endswith(extension):
            value = value[:-len(extension)]
            break
    return value.strip()


class GearBuilderIconSync:
    """每次只在内存中保留有限数量的小图标，不落整批本地文件。"""

    def __init__(
        self, *, size='medium', prefix='wow_icons_oss', workers=4, timeout=20,
        force=False, no_proxy=False, progress=None,
    ):
        if size not in ('tiny', 'small', 'medium'):
            raise GearBuilderIconSyncError(f'不支持的图标尺寸：{size}')
        self.size = size
        self.prefix = str(prefix or '').strip().strip('/')
        self.workers = max(1, min(12, int(workers or 4)))
        self.timeout = max(5, int(timeout or 20))
        self.force = bool(force)
        self.no_proxy = bool(no_proxy)
        self.proxies = (
            {'http': None, 'https': None, 'no_proxy': '*'}
            if self.no_proxy else _get_configured_proxies()
        )
        self.progress = progress or (lambda _message: None)
        if not self.prefix:
            raise GearBuilderIconSyncError('OSS 图标前缀不能为空。')
        config = getattr(settings, 'OSS_CONFIG', {}) or {}
        missing = [key for key in ('access_key_id', 'access_key_secret', 'region', 'bucket_name', 'base_url') if not config.get(key)]
        if missing:
            raise GearBuilderIconSyncError(f'OSS_CONFIG 缺少配置：{", ".join(missing)}')
        self.base_url = str(config['base_url']).rstrip('/')
        self.progress(
            '图标网络代理：'
            + (
                '已显式禁用'
                if self.no_proxy else
                ('使用项目 PROXY_CONFIG/REQUEST_CONFIG' if self.proxies else '未配置项目代理，遵循系统环境或直连')
            )
        )

    def sync(self, icon_names):
        iterator = (normalize_icon_name(value) for value in icon_names)
        pending = set()
        stats = {'processed': 0, 'uploaded': 0, 'skipped': 0, 'failed': 0, 'errors': []}
        exhausted = False

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            while pending or not exhausted:
                while not exhausted and len(pending) < self.workers * 2:
                    try:
                        icon_name = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    if not icon_name:
                        continue
                    pending.add(executor.submit(self._sync_one, icon_name))
                if not pending:
                    continue
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    stats['processed'] += 1
                    try:
                        status, icon_name, error = future.result()
                    except Exception as exc:
                        status, icon_name, error = 'failed', '未知图标', str(exc)
                    stats[status] += 1
                    if error:
                        stats['errors'].append(f'{icon_name}: {error}')
                    if stats['processed'] % 50 == 0:
                        self.progress(
                            f"图标进度 {stats['processed']}：上传 {stats['uploaded']}，"
                            f"已存在 {stats['skipped']}，失败 {stats['failed']}"
                        )
        self.progress(
            f"图标完成：处理 {stats['processed']}，上传 {stats['uploaded']}，"
            f"已存在 {stats['skipped']}，失败 {stats['failed']}"
        )
        stats['errors'] = stats['errors'][:100]
        return stats

    def _sync_one(self, icon_name):
        object_key = f'{self.prefix}/{self.size}/{icon_name}.jpg'
        public_url = self._public_url(object_key)
        if not self.force and self._object_exists(public_url):
            return 'skipped', icon_name, ''

        source_url = f'https://wow.zamimg.com/images/wow/icons/{self.size}/{quote(icon_name)}.jpg'
        try:
            response = requests.get(
                source_url,
                timeout=self.timeout,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; LMonitor-GearIcon/1.0)'},
                proxies=self.proxies,
            )
            content = response.content
        except requests.RequestException as exc:
            return 'failed', icon_name, f'下载失败：{exc}'
        if response.status_code != 200 or len(content) <= 100 or not content.startswith(b'\xff\xd8\xff'):
            return 'failed', icon_name, f'下载内容无效：HTTP {response.status_code}，{len(content)} 字节'
        try:
            uploaded_url = ossUploadBytes(content, object_key)
        except Exception as exc:
            return 'failed', icon_name, f'OSS 上传异常：{exc}'
        if not uploaded_url:
            return 'failed', icon_name, 'OSS 未返回上传地址'
        return 'uploaded', icon_name, ''

    def _object_exists(self, public_url):
        try:
            response = requests.head(
                public_url,
                timeout=min(self.timeout, 10),
                allow_redirects=True,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; LMonitor-GearIcon/1.0)'},
                proxies=self.proxies,
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def _public_url(self, object_key):
        encoded = '/'.join(quote(part) for part in object_key.split('/'))
        return f'{self.base_url}/{encoded}'
