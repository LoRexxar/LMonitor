"""装备按来源分支共享当前数据；构建号仅作来源记录。"""
from copy import copy
from copy import deepcopy
import re

from botend.templatetags.wow_tags import _normalize_icon_name


def named_item_icon(value):
    """Return a CDN basename, not a DB2 FileDataID or placeholder."""
    name = _normalize_icon_name(value)
    if (not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or name.isdigit()
            or name.casefold() == 'inv_misc_questionmark'):
        return ''
    return name


def current_fact_store(entries):
    """旧构建索引归并为每分支当前事实，兼容读取后写回单份资料。"""
    current = {}
    for key, entry in sorted(entries.items(), key=lambda pair: tuple(
            int(part) for part in pair[0].split('.')) if pair[0].replace('.', '').isdigit() else ()):
        if isinstance(entry, dict) and type(entry.get('is_ptr')) is bool and isinstance(entry.get('fact'), dict):
            current['ptr' if entry['is_ptr'] else 'retail'] = deepcopy(entry['fact'])
    current.update(deepcopy(entries.get('current_by_branch') or {}))
    return {'current_by_branch': current}


def variant_branch(variant):
    metadata = variant.metadata or {}
    return (metadata.get('data_branch') or ('ptr' if metadata.get('ptr_preview') else '')
            or getattr(variant, 'data_branch', 'retail'))


def branch_item(item, branch):
    """同一物品 ID 在不同 DBC 分支的名称和物理字段独立，不复制变体属性。"""
    payload = (item.metadata or {}).get('branch_display', {}).get(branch)
    if not isinstance(payload, dict):
        return item
    result = copy(item)
    allowed = {field.name for field in item._meta.concrete_fields} - {'id', 'item_id', 'metadata', 'updated_at'}
    for key in allowed & payload.keys():
        # A branch's raw FDID is not a replacement for a resolved icon name.
        # Keep the source payload intact for Journal's authorized /art path.
        if key == 'icon' and not named_item_icon(payload[key]) and named_item_icon(item.icon):
            continue
        setattr(result, key, payload[key])
    return result
