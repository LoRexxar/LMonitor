"""将手册引用的 Wago 客户端图片转换为可缓存的网页图片。"""
from io import BytesIO
from pathlib import Path

from django.conf import settings
from PIL import Image

from botend.services.journal_source import http_session


def cached_art(file_id):
    directory = Path(settings.BASE_DIR) / '.cache' / 'adventure-journal-art'
    path = directory / f'{int(file_id)}-v2.webp'
    if path.exists():
        return path
    previous = directory / f'{int(file_id)}.webp'
    if previous.exists():
        content = previous.read_bytes()
    else:
        with http_session() as session:
            response = session.get(f'https://wago.tools/api/casc/{int(file_id)}', timeout=(5, 20))
            response.raise_for_status()
            content = response.content
    if len(content) > 10 * 1024 * 1024:
        raise ValueError('手册图片超出大小限制')
    with Image.open(BytesIO(content)) as original:
        img = original.convert('RGBA')
        # 客户端纹理带有透明补齐区域，网页只展示有效画面。
        bounds = img.getchannel('A').getbbox()
        if bounds:
            img = img.crop(bounds)
        img.thumbnail((900, 600))
        output = BytesIO()
        img.save(output, 'WEBP', quality=85)
    directory.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output.getvalue())
    return path
