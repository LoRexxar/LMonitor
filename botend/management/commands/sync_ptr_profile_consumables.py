from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import SimcProfile, SimcTask
from botend.services.simc_player_config import (
    EQUIPMENT_SLOT_ALIASES,
    EQUIPMENT_SLOTS,
    canonical_simc_profile_key,
)


CONSUMABLE_KEYS = (
    'potion',
    'flask',
    'food',
    'augmentation',
    'temporary_enchant',
)


class Command(BaseCommand):
    help = (
        '按相同职业专精的 MID1 SimC 官方 Profile，为启用的 12.1 PTR Profile '
        '补齐缺失的药水、合剂、食物、增益符文和临时附魔。默认只预览，使用 --apply 才落库。'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='在没有待执行/执行中 SimC 任务时原子落库。',
        )

    @staticmethod
    def _line_key(line):
        stripped = str(line or '').strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            return ''
        return stripped.partition('=')[0].strip().lower()

    @classmethod
    def _official_consumables(cls, profile):
        found = {}
        for raw_line in str(profile.player_equipment or '').splitlines():
            key = cls._line_key(raw_line)
            if key not in CONSUMABLE_KEYS:
                continue
            if key in found:
                raise CommandError(
                    f'MID1 Profile #{profile.pk} {profile.name!r} 的 {key} 配置重复'
                )
            line = raw_line.strip()
            if not line.partition('=')[2].strip():
                raise CommandError(
                    f'MID1 Profile #{profile.pk} {profile.name!r} 的 {key} 配置为空'
                )
            found[key] = line
        missing = [key for key in CONSUMABLE_KEYS if key not in found]
        if missing:
            raise CommandError(
                f'MID1 Profile #{profile.pk} {profile.name!r} 缺少配置: {missing}'
            )
        return found

    @classmethod
    def _merge_missing_consumables(cls, player_equipment, official):
        original = str(player_equipment or '')
        lines = original.splitlines()
        present = {cls._line_key(line) for line in lines}
        additions = [official[key] for key in CONSUMABLE_KEYS if key not in present]
        if not additions:
            return original

        insert_at = None
        for index, line in enumerate(lines):
            if str(line or '').strip().lower() == '# gear':
                insert_at = index
                break
        if insert_at is None:
            equipment_keys = set(EQUIPMENT_SLOTS) | set(EQUIPMENT_SLOT_ALIASES)
            for index, line in enumerate(lines):
                if cls._line_key(line) in equipment_keys:
                    insert_at = index
                    break
        if insert_at is None:
            insert_at = len(lines)

        merged = '\n'.join(lines[:insert_at] + additions + lines[insert_at:])
        if original.endswith(('\n', '\r')):
            merged += '\n'
        return merged

    @classmethod
    def _build_plan(cls, *, lock=False):
        source_qs = SimcProfile.objects.filter(
            user_id__isnull=True,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            is_active=True,
        ).order_by('id')
        target_qs = SimcProfile.objects.filter(
            use_ptr=True,
            version='12.1',
            is_active=True,
        ).order_by('id')
        if lock:
            source_qs = source_qs.select_for_update()
            target_qs = target_qs.select_for_update()

        sources = {}
        for profile in source_qs:
            profile_key = canonical_simc_profile_key(profile.spec, profile.class_name)
            if not profile_key:
                raise CommandError(
                    f'MID1 Profile #{profile.pk} {profile.name!r} 无法解析职业专精'
                )
            if profile_key in sources:
                other = sources[profile_key]
                raise CommandError(
                    f'职业专精 {profile_key} 存在多个 MID1 Profile: '
                    f'#{other.pk}, #{profile.pk}'
                )
            sources[profile_key] = profile

        targets = list(target_qs)
        matched = []
        unmatched = []
        changed = []
        official_cache = {}
        for target in targets:
            profile_key = canonical_simc_profile_key(target.spec, target.class_name)
            source = sources.get(profile_key)
            if source is None:
                unmatched.append(target)
                continue
            official = official_cache.get(profile_key)
            if official is None:
                official = cls._official_consumables(source)
                official_cache[profile_key] = official
            merged = cls._merge_missing_consumables(target.player_equipment, official)
            matched.append(target)
            if merged != str(target.player_equipment or ''):
                changed.append((target, merged))
        return {
            'targets': targets,
            'matched': matched,
            'unmatched': unmatched,
            'changed': changed,
        }

    def _write_summary(self, prefix, plan):
        self.stdout.write(
            f'{prefix}: targets={len(plan["targets"])} '
            f'matched={len(plan["matched"])} '
            f'unmatched={len(plan["unmatched"])} '
            f'changed_profiles={len(plan["changed"])}'
        )
        for profile in plan['unmatched']:
            self.stdout.write(
                f'  unmatched Profile #{profile.pk}: {profile.name}'
            )

    def handle(self, *args, **options):
        if not options['apply']:
            plan = self._build_plan(lock=False)
            self._write_summary('DRY RUN', plan)
            self.stdout.write('使用 --apply 才会落库')
            return

        with transaction.atomic():
            if SimcTask.objects.select_for_update().filter(
                    current_status__in=(0, 1)).exists():
                raise CommandError(
                    '存在待执行或执行中的 SimC 任务，拒绝修改 12.1 PTR Profile'
                )
            plan = self._build_plan(lock=True)
            for profile, merged in plan['changed']:
                profile.player_equipment = merged
                profile.save(update_fields=['player_equipment'])

        self._write_summary('完成', plan)
