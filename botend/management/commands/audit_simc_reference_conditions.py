"""只读审计公开 APL；不创建任务、不刷新元数据、不改变发布状态。"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from botend.models import SimcApl, SimcReferencePublication, WowTalentVersion
from botend.services.simc_apl.condition_audit import audit_source
from botend.services.simc_apl.readable import display_symbols


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True, help='审计 JSON 文件路径')
        parser.add_argument('--spec', help='可选的专精标识；默认审计全部公开专精')
        parser.add_argument('--branch', choices=['retail', 'ptr'], default='retail')
        parser.add_argument('--fail-on-unknown', action='store_true')

    def handle(self, *args, **options):
        published = SimcReferencePublication.objects.filter(is_published=True)
        if options['spec']:
            published = published.filter(spec_key=options['spec'])
        pairs = published.values_list('spec_key', 'apls__id')
        pairs = list(dict.fromkeys((spec, pk) for spec, pk in pairs if pk))
        if not pairs:
            raise CommandError('没有待审计的公开 APL，未把空集合记作通过')
        apls = SimcApl.objects.filter(pk__in={pk for _, pk in pairs}, is_active=True).in_bulk()
        version = WowTalentVersion.objects.filter(is_active=True, branch=options['branch']).order_by(
            '-is_default_simulator', '-updated_at', '-id').first()
        contexts, reports = {}, []
        for spec, pk in pairs:
            if pk not in apls:
                continue
            if '_' not in spec:
                raise CommandError(f'专精标识无效：{spec}')
            if spec not in contexts:
                contexts[spec] = display_symbols(*spec.split('_', 1), version, branch=options['branch'])
            report = audit_source(apls[pk].content, f'{spec} / APL {pk} / {apls[pk].name}', contexts[spec])
            reports.append(report)
            self.stdout.write(f"{report['name']}：{report['expressions']} 个表达式，"
                              f"{report['unknown_expressions']} 个待解释，{len(report['identity_fallbacks'])} 处身份资料缺失")
        if not reports:
            raise CommandError('公开清单中没有启用的 APL，未把空集合记作通过')
        payload = {'mode': '数据库真实资料，只读', 'branch': options['branch'], 'files': reports}
        Path(options['output']).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        if options['fail_on_unknown'] and any(r['parse_issues'] or r['unknown_expressions'] for r in reports):
            raise CommandError('存在未解释条件或解析错误，明细已写入报告')
