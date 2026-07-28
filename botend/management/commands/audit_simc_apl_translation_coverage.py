import json
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError

from botend.dashboard.api import ConvertTextAPIView, _latest_catalog_identity
from botend.models import SimcApl
from botend.services.simc_apl.translation import extract_translation_demands


KINDS = ('action', 'buff', 'debuff', 'dot', 'cooldown', 'talent')


class Command(BaseCommand):
    help = (
        'Audit user-visible SimC APL Chinese coverage against every typed demand '
        'in the active system APL corpus.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--missing-limit', type=int, default=80,
            help='Maximum missing tokens returned per demand kind (default: 80).',
        )

    def handle(self, *args, **options):
        identity = _latest_catalog_identity()
        if not identity:
            raise CommandError('No active SimC runtime catalog is available')
        revision, wow_build = identity
        rows = list(SimcApl.objects.filter(
            is_active=True,
            is_system=True,
            sync_version=revision,
        ).order_by('spec').values('spec', 'content'))
        if not rows:
            raise CommandError(
                f'No active system APLs found for SimC revision {revision}')

        view = ConvertTextAPIView()
        counts = defaultdict(lambda: {
            'demand': 0,
            'mapped': 0,
            'missing': 0,
            'control': 0,
        })
        missing = defaultdict(Counter)
        per_spec = []
        for row in rows:
            demands = extract_translation_demands(row['content'])
            mapped = {
                (kind, token.casefold())
                for kind, token, _chinese in view.bilingual_pairs(
                    row['spec'], text=row['content'])[0]
            }
            local = defaultdict(lambda: {
                'demand': 0,
                'mapped': 0,
                'missing': 0,
                'control': 0,
            })
            for demand in demands:
                if demand.kind not in KINDS:
                    continue
                if demand.control:
                    counts[demand.kind]['control'] += 1
                    local[demand.kind]['control'] += 1
                    continue
                counts[demand.kind]['demand'] += 1
                local[demand.kind]['demand'] += 1
                key = (demand.kind, demand.token.casefold())
                if key in mapped:
                    counts[demand.kind]['mapped'] += 1
                    local[demand.kind]['mapped'] += 1
                else:
                    counts[demand.kind]['missing'] += 1
                    local[demand.kind]['missing'] += 1
                    missing[demand.kind][demand.token] += 1
            per_spec.append({'spec': row['spec'], 'by_kind': dict(local)})

        ordered_counts = {kind: counts[kind] for kind in KINDS}
        total_demand = sum(value['demand'] for value in ordered_counts.values())
        total_mapped = sum(value['mapped'] for value in ordered_counts.values())
        total_missing = sum(value['missing'] for value in ordered_counts.values())
        total_control = sum(value['control'] for value in ordered_counts.values())
        limit = max(0, options['missing_limit'])
        payload = {
            'identity': {'simc_revision': revision, 'wow_build': wow_build},
            'apl_count': len(rows),
            'by_kind': ordered_counts,
            'overall': {
                'demand': total_demand,
                'mapped': total_mapped,
                'missing': total_missing,
                'control': total_control,
                'coverage_pct': round(
                    (total_mapped * 100.0 / total_demand) if total_demand else 100.0,
                    2,
                ),
            },
            'missing_top': {
                kind: values.most_common(limit)
                for kind, values in missing.items()
            },
            'per_spec': per_spec,
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
