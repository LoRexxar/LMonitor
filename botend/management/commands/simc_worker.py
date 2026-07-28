from django.core.management.base import BaseCommand

from botend.simc_worker_entry import run_worker


class Command(BaseCommand):
    help = 'Run the dedicated SimC task worker'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='recover and consume at most one task')
        parser.add_argument('--poll-interval', type=float, default=None)

    def handle(self, *args, **options):
        run_worker(
            once=options.get('once', False),
            poll_interval=options.get('poll_interval'),
        )
