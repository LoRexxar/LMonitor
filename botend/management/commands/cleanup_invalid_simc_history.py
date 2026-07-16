from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from botend.models import SimcProfile, SimcTask, SimcTaskBatch
from botend.services.simc_player_config import validate_player_baseline


class Command(BaseCommand):
    help = '审计并清理缺少有效玩家基线、无法按当前 SimC Composer 执行的历史数据。默认 dry-run。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='实际删除；省略时只输出 dry-run 审计结果。',
        )
        parser.add_argument(
            '--user-id',
            type=int,
            default=None,
            help='只审计/清理指定用户 ID。',
        )

    @staticmethod
    def _invalid_profile_reason(profile):
        mode = str(profile.player_config_mode or '').strip().lower()
        if mode == 'battlenet':
            identity = (
                profile.battlenet_region,
                profile.battlenet_realm,
                profile.battlenet_character,
            )
            return '' if all(str(value or '').strip() for value in identity) else 'incomplete_battlenet_identity'
        if mode not in {'manual_equipment', 'attribute_only'}:
            return 'unsupported_player_config_mode'
        try:
            validate_player_baseline(profile.player_equipment)
        except ValueError:
            return 'missing_or_invalid_player_baseline'
        return ''

    @staticmethod
    def _has_trustworthy_task_state(task):
        return any((
            str(task.result_summary or '').strip(),
            str(task.result_file or '').strip(),
            str(task.final_simc_content or '').strip(),
            task.artifacts.exists(),
        ))

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        user_id = options.get('user_id')
        profiles = SimcProfile.objects.all().order_by('id')
        if user_id is not None:
            profiles = profiles.filter(user_id=user_id)

        invalid = []
        reasons = Counter()
        for profile in profiles.iterator():
            reason = self._invalid_profile_reason(profile)
            if reason:
                invalid.append(profile)
                reasons[reason] += 1

        invalid_profile_ids = [profile.id for profile in invalid]
        tasks = SimcTask.objects.filter(simc_profile_id__in=invalid_profile_ids).order_by('id')
        if user_id is not None:
            tasks = tasks.filter(user_id=user_id)
        deletable_tasks = [
            task for task in tasks.iterator()
            if task.current_status not in (0, 1) and not self._has_trustworthy_task_state(task)
        ]
        deletable_task_ids = [task.id for task in deletable_tasks]
        affected_batch_ids = sorted({task.batch_id for task in deletable_tasks if task.batch_id})

        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(
            f'{mode} invalid_profiles={len(invalid_profile_ids)} '
            f'deletable_tasks={len(deletable_task_ids)} candidate_batches={len(affected_batch_ids)}'
        )
        for reason, count in sorted(reasons.items()):
            self.stdout.write(f'  {reason}={count}')

        if not apply_changes:
            return

        with transaction.atomic():
            locked_tasks = SimcTask.objects.filter(id__in=deletable_task_ids).select_for_update()
            final_deletable = [
                task.id for task in locked_tasks
                if task.current_status not in (0, 1) and not self._has_trustworthy_task_state(task)
            ]
            if final_deletable:
                SimcTask.objects.filter(id__in=final_deletable).delete()

            locked_profiles = SimcProfile.objects.filter(id__in=invalid_profile_ids).select_for_update()
            final_profile_ids = [
                p.id for p in locked_profiles
                if not SimcTask.objects.filter(simc_profile_id=p.id, current_status__in=(0, 1)).exists()
            ]
            if final_profile_ids:
                SimcProfile.objects.filter(id__in=final_profile_ids).delete()

            empty_batches = SimcTaskBatch.objects.filter(id__in=affected_batch_ids).select_for_update()
            if user_id is not None:
                empty_batches = empty_batches.filter(user_id=user_id)
            empty_batch_ids = [
                batch.id for batch in empty_batches
                if not batch.simctask_set.exists()
            ]
            if empty_batch_ids:
                SimcTaskBatch.objects.filter(id__in=empty_batch_ids).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f'deleted_profiles={len(final_profile_ids)} '
                f'deleted_tasks={len(final_deletable)} '
                f'deleted_batches={len(empty_batch_ids)}'
            )
        )
