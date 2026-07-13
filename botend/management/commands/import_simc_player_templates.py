"""Import sanitized per-spec player baselines from SimC profiles/MID1."""
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import SimcContentTemplate
from botend.services.simc_player_config import validate_default_player_baseline, validate_player_baseline


DEFAULT_SOURCE_DIR = '/home/lighthouse/simc/profiles/MID1'
KNOWN_SPECS = {
    'deathknight': {'blood', 'frost', 'unholy'},
    'demonhunter': {'devourer', 'havoc', 'vengeance'},
    'druid': {'balance', 'feral', 'guardian', 'restoration'},
    'evoker': {'augmentation', 'devastation', 'preservation'},
    'hunter': {'beast_mastery', 'marksmanship', 'survival'},
    'mage': {'arcane', 'fire', 'frost'},
    'monk': {'brewmaster', 'mistweaver', 'windwalker'},
    'paladin': {'holy', 'protection', 'retribution'},
    'priest': {'discipline', 'holy', 'shadow'},
    'rogue': {'assassination', 'outlaw', 'subtlety'},
    'shaman': {'elemental', 'enhancement', 'restoration'},
    'warlock': {'affliction', 'demonology', 'destruction'},
    'warrior': {'arms', 'fury', 'protection'},
}
CLASS_NAMES = sorted(KNOWN_SPECS, key=len, reverse=True)
ALLOWED_SCALARS = {
    'level', 'race', 'region', 'server', 'realm', 'role', 'position', 'professions',
    'spec', 'talents', 'talent', 'omnium_talents', 'flask', 'food', 'potion',
    'augmentation', 'temporary_enchant', 'gear_strength', 'gear_crit', 'gear_haste',
    'gear_mastery', 'gear_versatility', 'gear_crit_rating', 'gear_haste_rating',
    'gear_mastery_rating', 'gear_versatility_rating',
}
EQUIPMENT = {
    'head', 'neck', 'shoulder', 'shoulders', 'back', 'chest', 'shirt', 'tabard',
    'wrist', 'wrists', 'hands', 'waist', 'legs', 'feet', 'finger1', 'finger2',
    'trinket1', 'trinket2', 'main_hand', 'off_hand',
}


class Command(BaseCommand):
    help = '从 SimC profiles/MID1 导入每个专精的默认玩家装备模板'

    def add_arguments(self, parser):
        parser.add_argument('--source-dir', default=DEFAULT_SOURCE_DIR)
        parser.add_argument('--sync-version', default='')
        parser.add_argument('--dry-run', action='store_true')

    @staticmethod
    def _parse_filename(filename):
        """Accept exactly MID1_Class_Spec.simc; hero suffixes cannot alias base specs."""
        normalized = filename.lower()
        class_tokens = {
            'deathknight': 'death_knight', 'demonhunter': 'demon_hunter',
        }
        for class_name, specs in KNOWN_SPECS.items():
            class_token = class_tokens.get(class_name, class_name)
            for spec in sorted(specs, key=len, reverse=True):
                if normalized == f'mid1_{class_token}_{spec}.simc':
                    return class_name, spec
        return None

    @staticmethod
    def _extract_baseline(content):
        lines = []
        actor_seen = False
        for raw in str(content or '').splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, _ = line.partition('=')
            if not sep:
                continue
            key = key.strip().lower()
            if key in CLASS_NAMES:
                if actor_seen:
                    raise ValueError('包含多个玩家 actor')
                actor_seen = True
                lines.append(line)
            elif key in ALLOWED_SCALARS or key in EQUIPMENT:
                lines.append(line)
        return validate_player_baseline('\n'.join(lines))

    def handle(self, *args, **options):
        source_dir = options['source_dir']
        if not os.path.isdir(source_dir):
            raise CommandError(f'MID1 目录不存在: {source_dir}')
        imported = skipped = errors = 0
        validated = []
        for filename in sorted(os.listdir(source_dir)):
            parsed = self._parse_filename(filename)
            if not parsed:
                if filename.lower().endswith('.simc'):
                    skipped += 1
                continue
            class_name, spec = parsed
            try:
                with open(os.path.join(source_dir, filename), encoding='utf-8') as source:
                    baseline = self._extract_baseline(source.read())
                baseline = validate_default_player_baseline(f'{class_name}_{spec}', baseline)
            except (OSError, ValueError) as exc:
                errors += 1
                self.stderr.write(self.style.ERROR(f'{filename}: {exc}'))
                continue
            spec_key = f'{class_name}_{spec}'
            validated.append((spec_key, class_name, baseline))
            if options['dry_run']:
                self.stdout.write(f'[DRY] {spec_key}: {len(baseline.splitlines())} 行')
            imported += 1

        if not options['dry_run'] and errors == 0:
            active_specs = [row[0] for row in validated]
            with transaction.atomic():
                SimcContentTemplate.objects.filter(
                    template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
                    source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                ).exclude(spec__in=active_specs).update(is_active=False)
                for spec_key, class_name, baseline in validated:
                    SimcContentTemplate.objects.update_or_create(
                        template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
                        source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                        spec=spec_key,
                        defaults={
                            'name': f'MID1 默认玩家 {spec_key}', 'class_name': class_name,
                            'content': baseline, 'sync_version': options['sync_version'],
                            'is_active': True, 'is_selectable': False,
                        },
                    )
        action = '预览' if options['dry_run'] else '导入'
        self.stdout.write(self.style.SUCCESS(f'{action}完成: {imported} 成功, {skipped} 跳过, {errors} 错误'))
