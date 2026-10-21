"""范围级中间结果缓存；完整内容指纹失效，损坏结果重新计算。"""
import json

from botend.models import SpecAggregationWorkCache
from django.db import connection
from botend.services.spec_stats_snapshot import content_digest


def load_work(season_id, scope, fingerprint):
    row = SpecAggregationWorkCache.objects.filter(
        season_id=season_id, scope=scope, fingerprint=fingerprint).first()
    if row is not None and content_digest(row.payload) == row.payload_hash:
        return row.payload
    return None


def save_work(season_id, scope, fingerprint, payload):
    from botend.controller.plugins.portal.SpecDetailAggregationMonitor import DecimalEncoder
    # 数据库JSON与最终文件使用相同的日期/小数编码。
    payload = json.loads(json.dumps(payload, cls=DecimalEncoder, ensure_ascii=False, allow_nan=False))
    SpecAggregationWorkCache.objects.bulk_create([SpecAggregationWorkCache(
        season_id=season_id, scope=scope, fingerprint=fingerprint,
        payload=payload, payload_hash=content_digest(payload))], update_conflicts=True,
        update_fields=['fingerprint', 'payload', 'payload_hash', 'updated_at'],
        unique_fields=['season', 'scope'] if connection.features.supports_update_conflicts_with_target else None)
    return payload
