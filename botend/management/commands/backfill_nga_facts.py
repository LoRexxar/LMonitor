"""Bounded source-only NGA refresh. No production writes without --apply."""
import time
from django.core.management.base import BaseCommand, CommandError
from botend.models import WowArticle, TargetAuth
from botend.services.nga_facts_service import fetch_page, parse_main_post, parse_listing, apply_facts

class Command(BaseCommand):
    help = 'Refresh NGA facts from current source HTML; default dry-run (network reads only).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--limit', type=int, default=20)
        parser.add_argument('--after-id', type=int, default=0)
        parser.add_argument('--listing-pages', type=int, default=1)
        order = parser.add_mutually_exclusive_group()
        order.add_argument('--newest', action='store_true', default=True,
                           help='Newest article IDs first (default).')
        order.add_argument('--oldest', action='store_false', dest='newest',
                           help='Ascending IDs, for resumable --after-id historical batches.')
        parser.add_argument('--listing-limit', type=int, default=500,
                            help='Maximum existing NGA records refreshed from listings, independent of --limit (1..2000).')

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 200 or not 1 <= options['listing_pages'] <= 5:
            raise CommandError('limit must be 1..200; listing-pages must be 1..5')
        if not 1 <= options['listing_limit'] <= 2000 or options['after_id'] < 0:
            raise CommandError('listing-limit must be 1..2000; after-id must be nonnegative')
        auth = TargetAuth.objects.filter(domain='bbs.nga.cn').first()
        cookie = auth.cookie if auth else ''
        listings = {}
        for fid in ('7', '310'):
            for page in range(1, options['listing_pages'] + 1):
                try:
                    rows = parse_listing(fetch_page(f'https://bbs.nga.cn/thread.php?fid={fid}&page={page}', cookie))
                    if not rows:
                        raise ValueError('No topic rows')
                    for row in rows:
                        listings[row['url']] = row['facts']
                except ValueError:
                    self.stderr.write(f'Listing fid={fid} page={page} unavailable; no fabricated facts')
                time.sleep(.3)
        # Listing facts are useful even outside the bounded main-body batch.
        # Never create rows here; the independent write cap is explicit.
        listing_matches = WowArticle.objects.filter(source='nga', url__in=listings).order_by('-id')
        listing_total = listing_matches.count()
        listing_candidates = 0
        for article in listing_matches[:options['listing_limit']]:
            facts = listings[article.url]
            if facts:
                listing_candidates += 1
                if options['apply']:
                    apply_facts(article, facts)
            self.stdout.write(f'listing id={article.id} fields={",".join(sorted(facts)) or "unresolved"}')
        changed = failed = processed = 0
        order = '-id' if options['newest'] else 'id'
        for article in WowArticle.objects.filter(source='nga', id__gt=options['after_id']).order_by(order)[:options['limit']]:
            processed += 1
            facts = {}
            try:
                main_facts = parse_main_post(fetch_page(article.url, cookie))
                if not main_facts:
                    raise ValueError('Main post unavailable')
                facts.update(main_facts)
            except ValueError:
                failed += 1
            if facts:
                changed += 1
                if options['apply']:
                    apply_facts(article, facts)
            self.stdout.write(f'id={article.id} fields={",".join(sorted(facts)) or "unresolved"}')
            time.sleep(.3)
        self.stdout.write(
            f'{"APPLY" if options["apply"] else "DRY-RUN"} '
            f'listing_urls={len(listings)} listing_matches={listing_total} '
            f'listing_candidates={listing_candidates} listing_applied={listing_candidates if options["apply"] else 0} '
            f'listing_skipped_by_limit={max(0, listing_total - options["listing_limit"])} '
            f'main_processed={processed} candidates={changed} fetch_failures={failed}; '
            'replies only refreshed from verified source facts; listing scope is independent of --limit/--after-id'
        )
