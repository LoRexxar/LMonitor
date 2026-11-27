"""Read-only release tool. Write stdout to a temporary file; install only on exit 0.

Only fixed public-game icon prefixes are listed. The bitmask groups avoid repeating
names shared by sizes. No DB, upload, HEAD, private key listing or request-time IO.
"""
import json
import re
import sys

SIZES = ('tiny', 'small', 'medium', 'large')
SLUG = re.compile(r'([A-Za-z0-9_-]{1,160})\.jpg', re.ASCII)


def inventory(client):
    from alibabacloud_oss_v2 import ListObjectsV2Request

    masks = {}
    for i, size in enumerate(SIZES):
        prefix = 'wow_icons_oss/' + size + '/'
        token = None
        seen = set()
        while True:
            page = client.list_objects_v2(ListObjectsV2Request(
                bucket='wowdail-cn', prefix=prefix, delimiter='/', max_keys=1000,
                continuation_token=token))
            if page.name != 'wowdail-cn' or page.prefix != prefix or page.is_truncated not in (True, False):
                raise ValueError('unexpected list response')
            for obj in page.contents or ():
                if not obj.key.startswith(prefix):
                    raise ValueError('unexpected object prefix')
                match = SLUG.fullmatch(obj.key[len(prefix):])
                if match and obj.size and obj.size > 0 and obj.object_type == 'Normal' and obj.storage_class in ('Standard', 'IA'):
                    name = match[1]
                    masks[name] = masks.get(name, 0) | (1 << i)
            if not page.is_truncated:
                break
            token = page.next_continuation_token
            if not token or token in seen:
                raise ValueError('invalid continuation')
            seen.add(token)
    if not masks:
        raise ValueError('refusing empty inventory')
    groups = {}
    for name in sorted(masks):
        groups.setdefault(masks[name], []).append(name)
    return {'schema': 1, 'sizes': SIZES, 'groups': {mask: ' '.join(names) for mask, names in sorted(groups.items())}}


def main():
    import alibabacloud_oss_v2 as oss
    from django.conf import settings

    cfg = settings.OSS_CONFIG
    if (cfg['bucket_name'], cfg['region'], cfg['base_url'].rstrip('/')) != (
            'wowdail-cn', 'cn-beijing', 'https://oss.wowdaily.cn'):
        raise ValueError('unexpected OSS configuration')
    conf = oss.config.load_default()
    conf.region, conf.endpoint = cfg['region'], 'https://oss-cn-beijing.aliyuncs.com'
    conf.enabled_redirect = conf.disable_ssl = conf.insecure_skip_verify = False
    conf.credentials_provider = oss.credentials.StaticCredentialsProvider(cfg['access_key_id'], cfg['access_key_secret'])
    result = inventory(oss.Client(conf))
    json.dump(result, sys.stdout, separators=(',', ':'))
    print()


if __name__ == '__main__':
    main()
