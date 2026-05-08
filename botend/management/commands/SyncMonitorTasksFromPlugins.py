from django.core.management.base import BaseCommand
from django.conf import settings as django_settings

from utils.log import logger

from LMonitor.config import Monitor_Type_BaseObject_List
from botend.plugin_sync import sync_monitortasks_from_plugin_list


class Command(BaseCommand):
    help = 'Sync MonitorTask from local plugin list'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Ignore MONITOR_TASK_AUTO_SYNC_PLUGINS switch')

    def handle(self, *args, **options):
        enabled = getattr(django_settings, 'MONITOR_TASK_AUTO_SYNC_PLUGINS', True)
        force = bool(options.get('force'))
        if not enabled and not force:
            logger.info('[MonitorTask Sync] Disabled by MONITOR_TASK_AUTO_SYNC_PLUGINS. Use --force to run anyway.')
            return

        created = sync_monitortasks_from_plugin_list(
            Monitor_Type_BaseObject_List,
            default_is_active=False,
            default_target="",
            skip_indexes={0},
        )
        logger.info(f'[MonitorTask Sync] Done. created={created}')
