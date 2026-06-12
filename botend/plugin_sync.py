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
    total = len(plugin_list or [])
    name_to_idx = {}
    for idx, plugin_cls in enumerate(plugin_list or []):
        if idx in skip:
            continue
        name = getattr(plugin_cls, "__name__", None) or f"PluginType{idx}"
        name_to_idx[str(name)] = idx

    with transaction.atomic():
        candidates = list(MonitorTask.objects.filter(name__in=list(name_to_idx.keys())))
        to_fix = []
        for t in candidates:
            desired = name_to_idx.get((t.name or "").strip())
            if desired is None:
                continue
            try:
                cur = int(getattr(t, "type", 0) or 0)
            except Exception:
                cur = 0
            if cur != int(desired):
                to_fix.append((int(t.id), int(desired)))

        for tid, _desired in to_fix:
            MonitorTask.objects.filter(id=tid).update(type=-tid)
        for tid, desired in to_fix:
            MonitorTask.objects.filter(id=tid).update(type=desired)

        for idx, plugin_cls in enumerate(plugin_list or []):
            if idx in skip:
                continue
            existing = MonitorTask.objects.filter(type=idx).first()
            if existing:
                name = getattr(plugin_cls, "__name__", None) or f"PluginType{idx}"
                if (existing.name or "").strip() != name:
                    logger.warning(f"[MonitorTask Sync] type={idx} name mismatch: db={existing.name} cfg={name}, updating...")
                    MonitorTask.objects.filter(id=existing.id).update(name=name)
                continue

            name = getattr(plugin_cls, "__name__", None) or f"PluginType{idx}"
            wait_time = 600
            if name in {"PortalPeakSpecRankMonitor", "PortalMplusCutoffMonitor"}:
                wait_time = 3600
            elif name == "SpecDetailSeasonMonitor":
                wait_time = 86400  # 24h
            elif name in {"SpecDetailPlayerMonitor", "SpecDetailRankingMonitor"}:
                wait_time = 43200  # 12h
            MonitorTask.objects.create(
                name=name,
                target=default_target,
                type=idx,
                is_active=default_is_active,
                wait_time=wait_time,
            )
            created += 1

    logger.info(f"[MonitorTask Sync] Done. created={created}, total_plugins={total}, skipped={len(skip)}")

    return created
