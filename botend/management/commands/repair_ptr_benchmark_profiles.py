from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import (
    SimcApl,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkSpec,
    SimcProfile,
    SimcTask,
)
from botend.services.simc_player_config import canonical_simc_profile_identity


TARGET_PANEL_SLUGS = (
    'ptr-12-1-default-scenarios',
    '121-ptr-0540ce8c2bbb435a832160525c4cb668',
)
TARGET_SPECS = {
    'hunter_marksmanship': {
        'class_name': 'hunter',
        'name': 'SimC 内置 PTR APL（射击猎）',
    },
    'hunter_survival': {
        'class_name': 'hunter',
        'name': 'SimC 内置 PTR APL（生存猎）',
    },
    'warlock_affliction': {
        'class_name': 'warlock',
        'name': 'SimC 内置 PTR APL（痛苦术）',
    },
}
OLD_AFFLICTION_RING = (
    'finger1=,id=268290,ilevel=334,gem_id=240890,enchant_id=7967,'
    'bonus_id=41/13668/13335/13786'
)
NEW_AFFLICTION_RING = (
    'finger1=,id=268249,ilevel=334,gem_id=240890,enchant_id=7967,'
    'bonus_id=40/13668/13335/12854'
)


class Command(BaseCommand):
    help = (
        '将两个 12.1 PTR Benchmark 面板的六个受影响 Profile 切换为 SimC 内置 PTR APL，'
        '并替换痛苦术未验证戒指。默认只预览，使用 --apply 才落库。'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='在通过全部前置校验且没有待执行/执行中 SimC 任务时原子落库。',
        )

    @staticmethod
    def _load_targets(*, lock=False):
        panel_qs = SimcBenchmarkPanel.objects
        spec_qs = SimcBenchmarkSpec.objects
        binding_qs = SimcBenchmarkProfile.objects
        profile_qs = SimcProfile.objects
        if lock:
            panel_qs = panel_qs.select_for_update()
            spec_qs = spec_qs.select_for_update()
            binding_qs = binding_qs.select_for_update()
            profile_qs = profile_qs.select_for_update()

        panels = list(panel_qs.filter(slug__in=TARGET_PANEL_SLUGS).order_by('slug'))
        found_slugs = {panel.slug for panel in panels}
        if found_slugs != set(TARGET_PANEL_SLUGS):
            missing = sorted(set(TARGET_PANEL_SLUGS) - found_slugs)
            raise CommandError(f'目标 Benchmark 面板缺失: {missing}')

        targets = []
        seen_profile_ids = set()
        for panel in panels:
            specs = list(spec_qs.filter(
                panel=panel,
                spec_key__in=TARGET_SPECS,
                is_enabled=True,
            ).order_by('spec_key'))
            found_specs = {panel_spec.spec_key for panel_spec in specs}
            if found_specs != set(TARGET_SPECS):
                missing = sorted(set(TARGET_SPECS) - found_specs)
                raise CommandError(f'面板 {panel.slug} 缺少目标专精: {missing}')

            for panel_spec in specs:
                bindings = list(binding_qs.filter(
                    panel_spec=panel_spec,
                    is_enabled=True,
                ).values_list('profile_id', flat=True))
                if len(bindings) != 1:
                    raise CommandError(
                        f'面板 {panel.slug} / {panel_spec.spec_key} 必须恰有一个启用 Profile，'
                        f'实际 {len(bindings)} 个'
                    )
                profile = profile_qs.get(pk=bindings[0])
                if profile.pk in seen_profile_ids:
                    raise CommandError(f'Profile #{profile.pk} 被目标面板重复绑定')
                seen_profile_ids.add(profile.pk)

                profile_class, profile_spec = canonical_simc_profile_identity(
                    profile.spec, profile.class_name,
                )
                canonical_spec = (
                    f'{profile_class}_{profile_spec}'
                    if profile_class and profile_spec else ''
                )
                if canonical_spec != panel_spec.spec_key:
                    raise CommandError(
                        f'Profile #{profile.pk} 专精不匹配: {canonical_spec!r} '
                        f'!= {panel_spec.spec_key!r}'
                    )
                if not profile.use_ptr or str(profile.version or '').strip() != '12.1':
                    raise CommandError(
                        f'Profile #{profile.pk} 不是显式 12.1 PTR Profile'
                    )

                if panel_spec.spec_key == 'warlock_affliction':
                    old_count = profile.player_equipment.count(OLD_AFFLICTION_RING)
                    new_count = profile.player_equipment.count(NEW_AFFLICTION_RING)
                    if (old_count, new_count) not in ((1, 0), (0, 1)):
                        raise CommandError(
                            f'Profile #{profile.pk} 痛苦术戒指状态不符合预期: '
                            f'old={old_count}, new={new_count}'
                        )
                targets.append((panel_spec, profile))

        if len(targets) != 6:
            raise CommandError(f'必须恰好命中六个 Profile，实际 {len(targets)} 个')
        return targets

    @staticmethod
    def _get_or_create_builtin_apls():
        result = {}
        for spec_key, definition in TARGET_SPECS.items():
            apl = SimcApl.objects.select_for_update().filter(
                source=SimcApl.SOURCE_SIMC_BUILTIN,
                spec=spec_key,
                is_system=True,
                owner_user_id__isnull=True,
                is_active=True,
            ).first()
            if apl is None:
                apl = SimcApl.objects.create(
                    name=definition['name'],
                    spec=spec_key,
                    class_name=definition['class_name'],
                    content='',
                    source=SimcApl.SOURCE_SIMC_BUILTIN,
                    is_system=True,
                    owner_user_id=None,
                    is_active=True,
                    is_selectable=True,
                    sync_version='',
                    validation_status=SimcApl.VALIDATION_DRAFT,
                )
            invalid_fields = []
            if apl.name != definition['name']:
                invalid_fields.append('name')
            if apl.class_name != definition['class_name']:
                invalid_fields.append('class_name')
            if apl.content != '':
                invalid_fields.append('content')
            if not apl.is_selectable:
                invalid_fields.append('is_selectable')
            if invalid_fields:
                raise CommandError(
                    f'已有 SimC 内置 APL #{apl.pk} 状态冲突: {invalid_fields}'
                )
            result[spec_key] = apl
        return result

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        if not apply_changes:
            targets = self._load_targets(lock=False)
            changed_profiles = sum(
                profile.player_equipment.count(OLD_AFFLICTION_RING) == 1
                for _, profile in targets
            )
            self.stdout.write(
                'DRY RUN: targets=6 changed_specs=6 '
                f'changed_profiles={changed_profiles}; 使用 --apply 才会落库'
            )
            return

        with transaction.atomic():
            if SimcTask.objects.select_for_update().filter(
                    current_status__in=(0, 1)).exists():
                raise CommandError('存在待执行或执行中的 SimC 任务，拒绝修改 PTR Benchmark 资源')

            targets = self._load_targets(lock=True)
            builtin_apls = self._get_or_create_builtin_apls()
            changed_specs = 0
            changed_profiles = 0
            for panel_spec, profile in targets:
                builtin_apl = builtin_apls[panel_spec.spec_key]
                if panel_spec.apl_id != builtin_apl.pk:
                    panel_spec.apl = builtin_apl
                    panel_spec.save(update_fields=['apl'])
                    changed_specs += 1

                if (panel_spec.spec_key == 'warlock_affliction'
                        and OLD_AFFLICTION_RING in profile.player_equipment):
                    profile.player_equipment = profile.player_equipment.replace(
                        OLD_AFFLICTION_RING, NEW_AFFLICTION_RING,
                    )
                    profile.save(update_fields=['player_equipment'])
                    changed_profiles += 1

        self.stdout.write(self.style.SUCCESS(
            f'完成: targets=6 changed_specs={changed_specs} '
            f'changed_profiles={changed_profiles}'
        ))
