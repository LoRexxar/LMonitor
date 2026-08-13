"""统一投影 WowItemSnapshot 的展示字段。"""
from __future__ import annotations

from botend.models import WowItemSnapshot
from botend.templatetags.wow_tags import wow_icon_oss_url


def item_display_metadata(item_id, snapshot=None):
    """Return the stable display contract shared by SimC equipment views."""
    try:
        normalized_id = int(item_id) if item_id not in (None, "") else None
    except (TypeError, ValueError):
        normalized_id = None
    name = (snapshot.name if snapshot else "") or ""
    name_zh = (snapshot.name_zh if snapshot else "") or ""
    description = (snapshot.description if snapshot else "") or ""
    description_zh = (snapshot.description_zh if snapshot else "") or ""
    icon = (snapshot.icon if snapshot else "") or ""
    return {
        "id": normalized_id,
        "item_id": normalized_id,
        "name": name,
        "name_zh": name_zh,
        "display_name": name_zh or name or (f"#{normalized_id}" if normalized_id else "未知物品"),
        "description": description,
        "description_zh": description_zh,
        "display_description": description_zh.strip() or description.strip(),
        "icon": icon,
        "icon_url": wow_icon_oss_url(icon) if icon else "",
        "quality": (snapshot.quality if snapshot else 0) or 0,
        "wowhead_url": f"https://www.wowhead.com/cn/item={normalized_id}" if normalized_id else "",
    }


def load_item_display_metadata(item_ids):
    """Bulk-load item display metadata keyed by numeric item ID."""
    normalized_ids = set()
    for item_id in item_ids or ():
        try:
            normalized_ids.add(int(item_id))
        except (TypeError, ValueError):
            continue
    if not normalized_ids:
        return {}
    snapshots = {
        int(row.item_id): row
        for row in WowItemSnapshot.objects.filter(item_id__in=normalized_ids)
    }
    return {
        item_id: item_display_metadata(item_id, snapshots.get(item_id))
        for item_id in normalized_ids
    }
