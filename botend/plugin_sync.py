from django.db import transaction

from utils.log import logger

from botend.models import MonitorTask


def sync_monitortasks_from_plugin_list(
    plugin_list,
    *,
    default_target="",
    default_is_active=False,
    skip_indexes=None,
):
    skip = set(skip_indexes or [])
    created = 0

    with transaction.atomic():
        for idx, plugin_cls in enumerate(plugin_list or []):
            if idx in skip:
                continue
            if MonitorTask.objects.filter(type=idx).exists():
                continue

            name = getattr(plugin_cls, "__name__", None) or f"PluginType{idx}"
            MonitorTask.objects.create(
                name=name,
                target=default_target,
                type=idx,
                is_active=default_is_active,
            )
            created += 1

    if created:
        logger.info(f"[MonitorTask Sync] Created {created} tasks from local plugins.")

    return created
