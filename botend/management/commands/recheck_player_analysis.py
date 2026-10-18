"""Execute exactly one previously queued, fenced analysis job."""
import json
import re
import signal
import resource

from django.core.management.base import BaseCommand, CommandError
from botend.services.player_analysis_recheck import run, Deadline, RUN_SECONDS


class Command(BaseCommand):
    help = '执行已排队的单角色重查；不创建任务、不接受 URL/force/AI 配置。'

    def add_arguments(self, parser):
        parser.add_argument('--analysis-id', type=int, required=True)
        parser.add_argument('--token', required=True)

    def handle(self, *args, **options):
        if options['analysis_id'] <= 0 or not re.fullmatch(r'[0-9a-f]{32}', options['token']):
            raise CommandError('无效的任务ID或token。')
        def timeout(signum, frame):
            raise Deadline()
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = 512 * 1024 * 1024
        if hard != resource.RLIM_INFINITY:
            ceiling = min(ceiling, hard)
        if soft != resource.RLIM_INFINITY:
            ceiling = min(ceiling, soft)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
        previous = signal.signal(signal.SIGALRM, timeout)
        signal.alarm(RUN_SECONDS)
        try:
            result = run(options['analysis_id'], options['token'])
            self.stdout.write(json.dumps(result, ensure_ascii=False))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
