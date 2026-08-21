from django.core.management.base import BaseCommand

from botend.models import SimcBackendBinary


class Command(BaseCommand):
    help = 'Release a production SimC update claim interrupted by a web-process restart.'

    def handle(self, *args, **options):
        message = '部署重启中断了进程内 SimC 更新，请重新触发'
        updated = SimcBackendBinary.objects.filter(
            identifier='production', is_updating=True,
        ).update(
            is_updating=False,
            update_status='部署重启已中断 SimC 更新',
            last_error=message,
        )
        if updated:
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write('没有需要恢复的 SimC 更新状态')
