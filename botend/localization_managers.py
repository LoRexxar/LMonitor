"""名称补充不应被天赋树、装备模拟和构建快照当作完整结构数据。"""
from botend.talent_identity import TalentIdentityManager


class TalentStructureManager(TalentIdentityManager):
    def get_queryset(self):
        return super().get_queryset().filter(localization_only=False, is_current=True)
