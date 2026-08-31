import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from botend.models import SimcBackendBinary, SimcProfile, SimcSkillDamageSnapshot
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService


class Command(BaseCommand):
    help = 'Generate a skill-damage snapshot, isolating every spec in a short-lived child process.'

    def add_arguments(self, parser):
        parser.add_argument('--snapshot-id', type=int, required=True)
        parser.add_argument('--backend-id', type=int)
        parser.add_argument('--profile-id', type=int)
        parser.add_argument('--output')
        parser.add_argument('--ready-file')

    def handle(self, *args, **options):
        snapshot = None
        skip_failure_update = False
        try:
            snapshot = SimcSkillDamageSnapshot.objects.filter(pk=options['snapshot_id']).first()
            if snapshot is None:
                raise CommandError('snapshot 不存在')
            backend = None
            if options.get('backend_id'):
                backend = SimcBackendBinary.objects.filter(pk=options['backend_id']).first()
                if backend is None:
                    raise CommandError('backend 不存在')
            service = SimcSkillDamageSnapshotService(snapshot, backend=backend)

            profile_id = options.get('profile_id')
            if profile_id:
                output = options.get('output')
                if not output:
                    raise CommandError('profile 子进程必须指定 --output')
                profile = SimcProfile.objects.filter(pk=profile_id).first()
                if profile is None:
                    raise CommandError('profile 不存在')
                result = service._generate_profile_product_actor(profile)
                Path(output).write_text(
                    json.dumps(result, ensure_ascii=False, separators=(',', ':')),
                    encoding='utf-8',
                )
                return

            lock_path = Path(tempfile.gettempdir()) / f'lmonitor-skill-snapshot-{snapshot.pk}.lock'
            lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
            lock_fd = os.open(lock_path, lock_flags, 0o600)
            with os.fdopen(lock_fd, 'w') as lock_file:
                lock_stat = os.fstat(lock_file.fileno())
                if lock_stat.st_uid != os.getuid() or not stat.S_ISREG(lock_stat.st_mode):
                    raise CommandError('快照生成锁文件不安全')
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    skip_failure_update = True
                    raise CommandError('该快照已有生成进程') from exc
                ready_file = options.get('ready_file')
                if ready_file:
                    Path(ready_file).write_text(str(snapshot.pk), encoding='utf-8')
                service.generate(isolate_profiles=True)
        except Exception as exc:
            if snapshot is not None and not skip_failure_update:
                SimcSkillDamageSnapshot.objects.filter(pk=snapshot.pk).exclude(
                    status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
                ).update(
                    status=SimcSkillDamageSnapshot.STATUS_FAILED,
                    error_text=str(exc)[:4000],
                    completed_at=timezone.now(),
                )
            if isinstance(exc, CommandError):
                raise
            raise CommandError(str(exc)) from exc
