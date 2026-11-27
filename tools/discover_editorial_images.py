"""Read only the published news index, registered reports and public video feed.

Run on the application host with its normal DB settings (or a read replica):
python -B -m tools.discover_editorial_images --root . > inventory.json
Use --urls for a one-URL-per-line publisher input. Neither mode contacts an image
host, lists buckets, persists article bodies, writes a database, or publishes.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from lxml import etree, html
from botend.services.site_image_assets import source_url

# Both the public index and active DB row must agree; unknown and UGC lack provenance.
NEWS = {'blizzard_cn', 'blizzard_tracker', 'wowhead', 'exwind', 'icy_veins'}
REPORT = re.compile(r'portal/reports/wow_daily_report_[0-9]{4}-[0-9]{2}-[0-9]{2}\.(?:md|html)')


def slots(text):
    if not text:
        return
    try:
        doc = html.fromstring(text, parser=html.HTMLParser(no_network=True))
    except (etree.ParserError, ValueError):
        return
    for e in doc.iter('img', 'source'):
        if e.tag == 'source' and (e.getparent() is None or e.getparent().tag != 'picture'):
            continue
        if any(re.search(r'(?:avatar|portrait|(?:^|[ -])nga(?:$|[ -]))',
                         ' '.join(node.get(k, '') for k in ('class', 'id', 'data-section')), re.I)
               for node in (e, *e.iterancestors())):
            continue
        for key in ('src', 'data-src'):
            if e.get(key):
                yield e.get(key)
        for key in ('srcset', 'data-srcset'):
            value = e.get(key, '')
            if any(c in value for c in '?#') or 'data:' in value.lower():
                continue
            for part in value.split(','):
                fields = part.strip().split()
                if len(fields) == 1 or (len(fields) == 2 and re.fullmatch(r'(?:[0-9]+w|[0-9]+(?:\.[0-9]+)?x)', fields[1])):
                    yield fields[0]


def block_slots(raw):
    blocks = json.loads(raw or '[]')
    for b in blocks if isinstance(blocks, list) else ():
        if not isinstance(b, dict):
            continue
        if b.get('type') == 'image':
            yield b.get('url')
        if b.get('type') == 'html':
            for k in ('html', 'original_html'):
                yield from slots(b.get(k))


def indexed(root):
    base = root / 'var/news'
    index = json.loads((base / 'index.json').read_bytes())
    seen, rows, totals = set(), defaultdict(list), Counter()
    for ref in index['shards']:
        name = ref['file']
        if not re.fullmatch('[0-9a-f]{64}\\.json', name):
            raise ValueError('invalid shard name')
        items = json.loads((base / name).read_bytes())['items']
        if len(items) != ref['count']:
            raise ValueError('shard count mismatch')
        for item in items:
            pk, source = int(item['id']), item['source']
            if pk in seen or pk <= 0:
                raise ValueError('invalid public index identity')
            seen.add(pk)
            totals[source] += 1
            if source in NEWS:
                rows[source].append(pk)
    if dict(totals) != index['sources']:
        raise ValueError('index source counts mismatch')
    return index['generation'], totals, rows


def scan(root, cur, video_since):
    generation, totals, rows = indexed(root)
    refs, counts, coverage = defaultdict(Counter), Counter(), Counter()

    def add(group, values):
        for raw in values:
            if not isinstance(raw, str):
                continue
            url = raw.strip()
            if url.startswith('//'):
                url = 'https:' + url
            if not source_url(url):
                counts['excluded_image_slots'] += 1
                continue  # never persist an unreviewed URL, including signatures
            if group == 'video' and not re.fullmatch(r'i[012]\.hdslb\.com', urlsplit(url).hostname or ''):
                continue
            refs[url][group] += 1

    for source, ids in rows.items():
        for i in range(0, len(ids), 12):
            batch = ids[i:i+12]
            cur.execute('SELECT content_blocks,content_blocks_cn FROM botend_wowarticle FORCE INDEX(PRIMARY) '
                        'WHERE is_active=1 AND source=%s AND id IN (' + ','.join(['%s'] * len(batch)) + ')',
                        (source, *batch))
            for original, translated in cur.fetchall():
                coverage['news:' + source] += 1
                for raw in (original, translated):
                    add('news:' + source, block_slots(raw))
    cur.execute('SELECT id,md_path FROM wow_daily_report ORDER BY id')
    for _, name in cur.fetchall():
        if not REPORT.fullmatch(name or ''):
            coverage['report:unreviewed_path'] += 1
            continue
        path = root / 'static' / name
        if not path.is_file() or path.is_symlink():
            coverage['report:missing'] += 1
            continue
        text = path.read_text(encoding='utf-8')
        coverage['report:' + path.suffix] += 1
        add('daily', slots(text))
        if path.suffix == '.md':
            add('daily', re.findall(r'!\[[^\]\n]*\]\((https?://[^()\s]+)\)', text))
    # Match PortalVideo feed eligibility/window; older declared covers are found in public reports.
    cur.execute("SELECT cover_url FROM wow_portal_video WHERE is_active=1 AND published_at >= %s "
                "AND title NOT LIKE %s ORDER BY published_at DESC,id DESC LIMIT 60", (video_since, '%【直播回放】%'))
    for cover, in cur.fetchall():
        coverage['video'] += 1
        add('video', [cover])
    return dict(generation=generation, index=totals, coverage=coverage, counters=counts, refs=refs)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path('.'), help='trusted production snapshot/application root')
    ap.add_argument('--urls', action='store_true', help='print only unique admissible public source URLs')
    args = ap.parse_args()
    import MySQLdb
    from django.conf import settings
    from django.db import connection
    from django.utils import timezone
    cfg = settings.DATABASES['default']
    with closing(MySQLdb.connect(host=cfg['HOST'], port=int(cfg.get('PORT') or 3306), user=cfg['USER'],
                                 passwd=cfg['PASSWORD'], db=cfg['NAME'], charset='utf8mb4',
                                 connect_timeout=5, read_timeout=15)) as conn, closing(conn.cursor()) as cur:
        cur.execute('SET SESSION TRANSACTION READ ONLY')
        conn.autocommit(True)
        since = connection.ops.adapt_datetimefield_value(timezone.now() - timedelta(days=3))
        report = scan(args.root, cur, since)
    if args.urls:
        for url in sorted(report['refs']):
            print(url)
    else:
        print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
