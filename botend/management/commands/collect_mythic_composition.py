from django.core.management.base import BaseCommand

from botend.services.mythic_composition_jobs import collect


class Command(BaseCommand):
    help = '执行已提交的五人阵容查询任务。'

    def add_arguments(self, parser):
        parser.add_argument('job_id', type=int)

    def handle(self, job_id, **options):
        collect(job_id)
