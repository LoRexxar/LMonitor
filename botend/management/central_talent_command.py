"""旧采集命令统一发布中央展示，嵌套命令与候选导入不重复发布。"""
from contextvars import ContextVar

from django.core.management.base import BaseCommand

from botend.models import WowTalentVersion

_depth = ContextVar('central_talent_command_depth', default=0)


class CentralTalentCommand(BaseCommand):
    def execute(self, *args, **options):
        depth = _depth.get()
        token = _depth.set(depth + 1)
        try:
            result = super().execute(*args, **options)
            preview = options.get('dry_run') or ('apply' in options and not options['apply']) or (
                'write' in options and not options['write'])
            keys = options.get('version_key') or []
            keys = [keys] if isinstance(keys, str) else keys
            # 候选表仅用于完整性校验，回滚前不能触碰当前分支展示资料。
            staging = keys and WowTalentVersion.objects.filter(key__in=keys, status='staging').exists()
            if not depth and not preview and not staging:
                from botend.services.wow_spell_display import sync_spell_display
                branches = set(WowTalentVersion.objects.filter(key__in=keys).values_list('branch', flat=True)) if keys else {'retail', 'ptr'}
                if not keys and options.get('branch') in ('retail', 'ptr'):
                    branches = {options['branch']}
                for branch in sorted(branches):
                    sync_spell_display(branch=branch, apply=True)
            return result
        finally:
            _depth.reset(token)
