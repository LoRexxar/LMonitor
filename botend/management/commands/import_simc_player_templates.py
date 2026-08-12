"""Import sanitized per-spec player baselines from a SimC profile set."""
import os
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import SimcProfile
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
MID1_UNSUPPORTED_PROFILE_SPECS = {
    ('druid', 'restoration'), ('evoker', 'augmentation'),
    ('evoker', 'preservation'), ('monk', 'mistweaver'),
    ('paladin', 'holy'), ('priest', 'discipline'),
    ('priest', 'holy'), ('shaman', 'restoration'),
}
REQUIRED_PROFILE_SPECS = {
    (class_name, spec)
    for class_name, specs in KNOWN_SPECS.items()
    for spec in specs
} - MID1_UNSUPPORTED_PROFILE_SPECS
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
        parser.add_argument('--profile-set', default='', help='上游 Profile 集合，如 MID1/MID2')
        parser.add_argument('--profile-version', default='', help='Profile 游戏版本，如 12.0/12.1')
        parser.add_argument('--use-ptr', action='store_true', help='将本次导入的 Profile 显式标记为 PTR')
        parser.add_argument('--dry-run', action='store_true')

    @staticmethod
    def _parse_filename(filename, profile_set='MID1'):
        """Accept exactly <profile_set>_Class_Spec.simc; hero suffixes cannot alias base specs."""
        normalized = filename.lower()
        prefix = str(profile_set or 'MID1').lower()
        class_tokens = {
            'deathknight': 'death_knight', 'demonhunter': 'demon_hunter',
        }
        for class_name, specs in KNOWN_SPECS.items():
            class_token = class_tokens.get(class_name, class_name)
            for spec in sorted(specs, key=len, reverse=True):
                if normalized == f'{prefix}_{class_token}_{spec}.simc':
                    return class_name, spec
        return None

    @staticmethod
    def _normalize_equipment_line(line):
        key, sep, raw_value = line.partition('=')
        if not sep or key.strip().lower() not in EQUIPMENT:
            return line
        match = re.fullmatch(r'\s*(\d+)\s*(,.*)?', raw_value)
        if not match:
            return line
        suffix = match.group(2) or ''
        return f'{key.strip()}=,id={match.group(1)}{suffix}'

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
                lines.append(Command._normalize_equipment_line(line))
        return validate_player_baseline('\n'.join(lines))

    def handle(self, *args, **options):
        source_dir = options['source_dir']
        profile_set = str(options.get('profile_set') or 'MID1').upper()
        if profile_set not in {'MID1', 'MID2'}:
            raise CommandError(f'不支持的 Profile 集合: {profile_set}')
        profile_version = str(options.get('profile_version') or ('12.1' if profile_set == 'MID2' else '12.0'))
        if not os.path.isdir(source_dir):
            raise CommandError(f'Profile 目录不存在: {source_dir}')
        imported = skipped = errors = 0
        validated = []
        seen = set()
        for filename in sorted(os.listdir(source_dir)):
            parsed = self._parse_filename(filename, profile_set)
            if not parsed:
                if filename.lower().endswith('.simc'):
                    skipped += 1
                continue
            class_name, spec = parsed
            if parsed in seen:
                errors += 1
                self.stderr.write(self.style.ERROR(f'{filename}: 重复专精基线'))
                continue
            seen.add(parsed)
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

        missing_specs = REQUIRED_PROFILE_SPECS - seen
        if missing_specs:
            missing = ', '.join(f'{class_name}_{spec}' for class_name, spec in sorted(missing_specs))
            errors += len(missing_specs)
            self.stderr.write(f'缺少专精基线: {missing}')
        if not options['dry_run'] and errors == 0:
            active_keys = [f'simc_upstream:{row[0]}' for row in validated]
            with transaction.atomic():
                SimcProfile.objects.filter(
                    user_id__isnull=True,
                    source=SimcProfile.SOURCE_SIMC_UPSTREAM,
                    system_key__startswith='simc_upstream:',
                ).delete()
                for spec_key, class_name, baseline in validated:
                    SimcProfile.objects.update_or_create(
                        system_key=f'simc_upstream:{spec_key}',
                        defaults={
                            'user_id': None,
                            'source': SimcProfile.SOURCE_SIMC_UPSTREAM,
                            'name': f'{profile_set} 默认玩家 {spec_key}', 'class_name': class_name,
                            'version': profile_version, 'profile_set': profile_set,
                            'spec': spec_key, 'player_config_mode': 'manual_equipment',
                            'use_ptr': bool(options.get('use_ptr', False)),
                            'player_equipment': baseline, 'talent': '',
                            'gear_strength': None, 'gear_crit': None,
                            'gear_haste': None, 'gear_mastery': None,
                            'gear_versatility': None,
                            'sync_version': options['sync_version'], 'is_active': True,
                        },
                    )
        action = '预览' if options['dry_run'] else '导入'
        if errors:
            raise CommandError(f'{action}失败: {imported} 成功, {skipped} 跳过, {errors} 错误')
        self.stdout.write(self.style.SUCCESS(f'{action}完成: {imported} 成功, {skipped} 跳过, {errors} 错误'))
