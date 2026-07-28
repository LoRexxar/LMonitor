import argparse
import signal
from typing import Any, Callable, Optional

from botend.services.simc_worker import SimcWorker


def run_worker(
    *,
    once: bool = False,
    poll_interval: Optional[float] = None,
    worker_factory: Optional[Callable[..., Any]] = None,
):
    """Run the dedicated SimC consumer without depending on Django's CLI."""
    factory = worker_factory or SimcWorker
    worker = factory(poll_interval=poll_interval)
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)

    if once:
        worker.recover_stale_tasks()
        worker.consume_once()
        return

    worker.run()


def build_parser():
    parser = argparse.ArgumentParser(description='Run the dedicated SimC task worker')
    parser.add_argument(
        '--once',
        action='store_true',
        help='recover stale leases and consume at most one task',
    )
    parser.add_argument('--poll-interval', type=float, default=None)
    return parser


def main(argv=None):
    options = build_parser().parse_args(argv)
    run_worker(once=options.once, poll_interval=options.poll_interval)
