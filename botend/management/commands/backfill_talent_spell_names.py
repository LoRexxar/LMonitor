# -*- coding: utf-8 -*-

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowWagoMonitorState


class Command(BaseCommand):
    help = '使用 wago.tools DB2 回填天赋相关 spell 中文名，并同步到天赋元数据缓存'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--spec-name', default='', help='仅处理指定专精')
        parser.add_argument('--build', default='', help='显式指定 Wago build，例如 12.0.7.67525')
        parser.add_argument('--branch', default='wow', help='Wago branch，默认 wow')
        parser.add_argument('--limit', type=int, default=200, help='最多处理多少个 spell_id')
        parser.add_argument('--chunk-size', type=int, default=50, help='每批处理的 spell_id 数量')

    def handle(self, *args, **options):
        class_name = options['class_name']
        spec_name = options['spec_name']
        branch = options['branch']
        limit = max(1, int(options['limit']))
        chunk_size = max(1, min(int(options['chunk_size']), 200))
        build = (options['build'] or '').strip() or self._guess_build(branch)

        if not build:
            raise CommandError('无法推断可用 build，请使用 --build 显式指定')

        monitor = WagoSkillDiffMonitor(None, None)
        queryset = WowTalentNodeMetadata.objects.filter(
            Q(display_spell_id__isnull=True) |
            Q(name='') |
            Q(name='未命名天赋') |
            Q(name__startswith='技能ID ')
        ).exclude(spell_id__isnull=True)
        if class_name:
            queryset = queryset.filter(class_name=class_name)
        if spec_name:
            queryset = queryset.filter(spec_name=spec_name)

        rows = list(queryset.order_by('id')[:limit])
        if not rows:
            self.stdout.write(self.style.WARNING('没有找到需要回填名称的 spell_id'))
            return

        self.stdout.write(f'使用 build {build} 解析 {len(rows)} 条天赋节点')

        resolved_rows = []
        for row in rows:
            resolved = self._resolve_metadata_row(monitor, build, row)
            if resolved:
                resolved_rows.append((row, resolved))

        if not resolved_rows:
            raise CommandError('未回填到任何 spell 名称，可能是网络不可达或 build 不匹配')

        now = timezone.now()
        snapshot_spell_ids = {}
        for _, resolved in resolved_rows:
            spell_id = resolved.get('display_spell_id')
            name_zh = (resolved.get('name_zh') or '').strip()
            if not spell_id or not name_zh:
                continue
            snapshot_spell_ids[int(spell_id)] = name_zh

        for spell_id, name_zh in snapshot_spell_ids.items():
            WowSpellSnapshot.objects.update_or_create(
                branch=branch,
                locale=monitor.locale,
                spell_id=int(spell_id),
                defaults={
                    'name_zh': (name_zh or '')[:255],
                    'snapshot_build': build,
                    'updated_at': now,
                }
            )

        updated = 0
        for row, resolved in resolved_rows:
            changed = False
            for field in ['display_spell_id', 'name', 'name_zh', 'max_points']:
                value = resolved.get(field)
                if value in (None, '', []):
                    continue
                if getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True
            if changed:
                row.last_updated = now
                row.save(update_fields=['display_spell_id', 'name', 'name_zh', 'max_points', 'last_updated'])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'已通过 trait 映射回填 {len(snapshot_spell_ids)} 个真实 spell 名称，更新 {updated} 条天赋元数据'
        ))

    def _resolve_metadata_row(self, monitor, build, row):
        raw_id = row.node_id or row.talent_id or row.spell_id
        if not raw_id:
            return {}

        entry = monitor._fetch_db2_row_by_id_requests('TraitNodeEntry', build, raw_id)
        if entry:
            definition_id = int(entry.get('TraitDefinitionID') or 0)
            max_ranks = int(entry.get('MaxRanks') or 1)
            definition = monitor._fetch_db2_row_by_id_requests('TraitDefinition', build, definition_id) if definition_id else {}
            display_spell_id = int(
                definition.get('VisibleSpellID')
                or definition.get('SpellID')
                or definition.get('OverridesSpellID')
                or 0
            )
            if display_spell_id > 0:
                name_zh = self._resolve_spell_name(monitor, build, display_spell_id)
                if name_zh:
                    return {
                        'display_spell_id': display_spell_id,
                        'name': name_zh,
                        'name_zh': name_zh,
                        'max_points': max_ranks,
                    }

        direct_spell_id = int(row.spell_id or 0)
        if direct_spell_id > 0:
            name_zh = self._resolve_spell_name(monitor, build, direct_spell_id)
            if name_zh:
                return {
                    'display_spell_id': direct_spell_id,
                    'name': name_zh,
                    'name_zh': name_zh,
                    'max_points': row.max_points or 1,
                }
        return {}

    @staticmethod
    def _resolve_spell_name(monitor, build, spell_id):
        row = monitor._fetch_db2_row_by_id_requests('SpellName', build, spell_id, locale_override=monitor.name_locale)
        name = (row.get('Name_lang') or '').strip()
        if name:
            return name
        return (monitor._fetch_spell_name_wowhead_cn(spell_id) or '').strip()

    @staticmethod
    def _guess_build(branch):
        state = WowWagoMonitorState.objects.filter(branch=branch, is_active=True).order_by('-updated_at').first()
        if state and (state.build or '').strip():
            return state.build.strip()

        latest = WowSpellSnapshot.objects.exclude(snapshot_build='').order_by('-updated_at').first()
        if latest and (latest.snapshot_build or '').strip():
            return latest.snapshot_build.strip()
        return ''
