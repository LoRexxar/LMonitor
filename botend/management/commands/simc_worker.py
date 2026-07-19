import signal

from django.core.management.base import BaseCommand

from botend.services.simc_worker import SimcWorker


class Command(BaseCommand):
    help = 'Run the dedicated SimC task worker'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='recover and consume at most one task')
        parser.add_argument('--poll-interval', type=float, default=None)

    def handle(self, *args, **options):
        worker = SimcWorker(poll_interval=options.get('poll_interval'))
        signal.signal(signal.SIGINT, worker.request_stop)
        signal.signal(signal.SIGTERM, worker.request_stop)
        worker.recover_stale_tasks()
        if options.get('once'):
            worker.consume_once()
            return
        worker.run()
