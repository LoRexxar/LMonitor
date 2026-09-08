"""名称补充不应被天赋树、装备模拟和构建快照当作完整结构数据。"""
from django.db import models


class TalentStructureManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(localization_only=False)
