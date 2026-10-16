"""技能原始快照按唯一身份和实际字段差异写入，不以采集时间制造变化。"""
from django.db import transaction
from django.db.models import Q


@transaction.atomic
def upsert_changed_snapshots(model, objs, unique_fields, update_fields, batch_size=None):
    if batch_size is not None and batch_size < 1:
        raise ValueError('批次大小必须大于零')
    # 同一批同号资料只采用最后一条，与原有更新冲突语义一致。
    candidates = {tuple(getattr(row, key) for key in unique_fields): row for row in objs}
    if not candidates:
        return 0
    fields = [field for field in update_fields if field != 'updated_at']
    size = min(batch_size or 100, 100)
    items = list(candidates.items())
    written = 0
    for start in range(0, len(items), size):
        chunk = items[start:start + size]
        query = Q()
        for key, _ in chunk:
            query |= Q(**dict(zip(unique_fields, key)))
        existing = {tuple(getattr(row, field) for field in unique_fields): row
                    for row in model.objects.select_for_update().filter(query)}
        creates, updates = [], []
        for key, row in chunk:
            old = existing.get(key)
            if old is None:
                creates.append(row)
            elif any(getattr(old, field) != getattr(row, field) for field in fields):
                for field in update_fields:
                    setattr(old, field, getattr(row, field))
                updates.append(old)
        if creates:
            # 数据库唯一约束处理并发新行；冲突须回滚重试，不能静默丢失更新。
            model.objects.bulk_create(creates, batch_size=size)
        if updates:
            model.objects.bulk_update(updates, update_fields, batch_size=size)
        written += len(creates) + len(updates)
    return written
