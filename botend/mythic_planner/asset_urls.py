from urllib.parse import unquote, urlsplit


WOWHEAD_ASSET_HOSTS = {
    'wow.zamimg.com',
    'wowhead.com',
    'www.wowhead.com',
}

LEGACY_OSS_HOSTS = {
    'oss.shengnong.club',
}


def normalize_asset_source_url(value):
    """把历史 OSS 套娃地址还原为可追溯的原始图片地址。"""

    original = str(value or '').strip()
    current = original
    for _ in range(8):
        parsed = urlsplit(current)
        host = (parsed.hostname or '').lower()
        if parsed.scheme not in {'http', 'https'} or not host:
            return original

        path = unquote(parsed.path or '')
        parts = [part for part in path.strip('/').split('/') if part]
        if host in WOWHEAD_ASSET_HOSTS:
            return 'https://wow.zamimg.com/' + '/'.join(parts)

        source_index = next(
            (
                index
                for index, part in enumerate(parts[:-1])
                if part.lower() == 'sources'
            ),
            None,
        )
        if source_index is not None:
            embedded_host = parts[source_index + 1].lower()
            embedded_path = '/'.join(parts[source_index + 2:])
            if embedded_host and embedded_path:
                current = f'https://{embedded_host}/{embedded_path}'
                continue

        wowhead_index = next(
            (
                index
                for index, part in enumerate(parts[:-1])
                if part.lower() == 'wowhead'
            ),
            None,
        )
        if wowhead_index is not None:
            wowhead_path = '/'.join(parts[wowhead_index + 1:])
            if wowhead_path:
                current = f'https://wow.zamimg.com/{wowhead_path}'
                continue
        return original
    return original


def references_legacy_oss(value):
    """判断地址本身或任意归档层级是否包含废弃 OSS 域名。"""

    parsed = urlsplit(str(value or '').strip())
    host = (parsed.hostname or '').lower()
    if host in LEGACY_OSS_HOSTS:
        return True
    parts = {
        part.lower()
        for part in unquote(parsed.path or '').strip('/').split('/')
        if part
    }
    return bool(parts & LEGACY_OSS_HOSTS)
