"""测试显式发布资料，不在业务逻辑中提供自动全选行为。"""
from botend.models import SimcReferencePublication


def publish_resources(spec_key, *, profiles=(), talents=(), apls=()):
    row, _ = SimcReferencePublication.objects.get_or_create(spec_key=spec_key)
    row.is_published = True
    row.save(update_fields=['is_published'])
    row.profiles.add(*profiles)
    row.talents.add(*talents)
    row.apls.add(*apls)
    return row
