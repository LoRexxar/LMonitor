import signal

from django.core.management.base import BaseCommand

from botend.services.gear_assistant_jobs import TIMEOUT, run_job


def run_with_deadline(job_id, seconds=TIMEOUT - 5):
    # The kernel must enforce the deadline even if the launcher dies or the
    # solver is blocked in a C extension. Arm it before the database claim.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        run_job(job_id)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


class Command(BaseCommand):
    help = 'Claim and execute a queued auxiliary gear calculation.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('job_id', type=int)

    def handle(self, job_id, **options):
        run_with_deadline(job_id)
