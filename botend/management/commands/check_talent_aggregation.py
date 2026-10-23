"""只读检查当前节点、0254遗留行及正式聚合入口，不采集、不发布、不清理。"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import SeasonMeta, WowTalentNodeMetadata
from botend.services.spec_stats_snapshot import read_projection, _validated_details


class Command(BaseCommand):
    help = '检查当前天赋与正式聚合；--require-current 将缺失、旧版或待重建视为失败'

    def add_arguments(self, parser):
        parser.add_argument('--season', type=int)
        parser.add_argument('--class', dest='class_name')
        parser.add_argument('--spec', dest='spec_name')
        parser.add_argument('--database-only', action='store_true')
        parser.add_argument('--require-current', action='store_true')

    def handle(self, *args, **options):
        rows = WowTalentNodeMetadata.all_objects.all()
        retired = rows.filter(localization_only=False, is_current=False).count()
        kept = rows.exclude(localization_only=False, is_current=False)
        duplicates = list(kept.filter(talent_version_id__isnull=False, node_id__isnull=False,
            spell_id__isnull=False).values('talent_version_id', 'class_name', 'spec_name',
            'tree_type', 'node_id', 'spell_id').annotate(total=Count('pk')).filter(total__gt=1)[:10])
        report = {'退役结构节点': retired, '当前唯一约束冲突': duplicates, '聚合': []}
        failures = bool(retired or duplicates)
        if not options['database_only']:
            season = (SeasonMeta.objects.filter(pk=options['season']).first() if options['season']
                      else SeasonMeta.objects.filter(is_active=True).first())
            if not season:
                raise CommandError('未找到赛季')
            scopes = [(cls, spec) for cls, specs in CLASS_SPEC_MAP.items() for spec in specs
                      if (not options['class_name'] or options['class_name'] == cls)
                      and (not options['spec_name'] or options['spec_name'] == spec)]
            if not scopes:
                raise CommandError('未找到指定职业/专精')
            root = Path(getattr(settings, 'MEDIA_ROOT', '') or 'media') / 'aggregated' / str(season.pk)
            modules = []
            if season.mplus_encounters:
                modules.append('dungeon')
            if season.raid_zones or season.raid_encounters:
                modules.append('raid')
            for cls, spec in scopes:
                for module in modules:
                    path = root / cls / spec / (module + '.json')
                    index = read_projection(path)
                    status, checked = '当前', 0
                    if module == 'dungeon':
                        required = {f"dungeon-{enc['id']}" for enc in season.mplus_encounters} | {'dungeon-all'}
                    else:
                        encounters = ([enc for zone in season.raid_zones for enc in zone.get('encounters', [])]
                                      if season.raid_zones else season.raid_encounters)
                        required = {f"raid-{difficulty}-{enc['id']}" for difficulty in (4, 5) for enc in encounters}
                    if not index:
                        status = '缺失或损坏'
                    elif not index.get('_snapshot', {}).get('talent_structure_token'):
                        status = '旧版，需重建'
                    elif set(index['_snapshot']['details']) != required:
                        status = '详情范围不完整，需重建'
                    elif _validated_details(path, index, index['_snapshot']['details']) is None:
                        status = '详情缺失或校验失败'
                    else:
                        for key in index['_snapshot']['details']:
                            value = read_projection(path, key)
                            if not value or value.get('_stats_refresh_required'):
                                status = '身份变化或损坏，需重建'
                                break
                            checked += 1
                    failures |= status != '当前'
                    report['聚合'].append({'范围': f'{cls}/{spec}/{module}', '状态': status, '已核验详情': checked})
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if duplicates:
            raise CommandError('当前节点存在约束冲突，停止迁移并核对报告')
        if options['require_current'] and failures:
            raise CommandError('验收未通过：存在历史节点、约束冲突或需要重建的聚合')
