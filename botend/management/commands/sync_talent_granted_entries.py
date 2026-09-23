"""Synchronize per-spec default talent Entry IDs from an exact-build DB2 dump."""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.wow.talents.grants import DB2_GRANT_TABLES, derive_granted_entries
from botend.wow.talents.metadata import AUTHORITATIVE_TALENT_SOURCES


class Command(BaseCommand):
    help = '按同 build 的 TraitCond / SpecSetMember 同步职业树默认赠送天赋；默认只读预览'

    def add_arguments(self, parser):
        parser.add_argument('--version-key', required=True)
        parser.add_argument('--build', required=True)
        parser.add_argument('--dump-dir', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        build = options['build'].strip()
        version = WowTalentVersion.objects.get(key=options['version_key'])
        if not build or version.current_build != build:
            raise CommandError(f'目标版本 build={version.current_build!r} 与 DB2 build={build!r} 不一致')
        dump_dir = Path(options['dump_dir'])
        # A dump under a different build directory must not be silently applied.
        if dump_dir.name != build:
            raise CommandError('DB2 目录末级必须与 --build 完全一致')
        tables = {}
        for name in DB2_GRANT_TABLES:
            path = dump_dir / (name + '.csv')
            if not path.is_file():
                raise CommandError(f'缺少 DB2 表 {path}')
            with path.open(newline='', encoding='utf-8-sig') as stream:
                tables[name] = list(csv.DictReader(stream))
            if not tables[name]:
                raise CommandError(f'DB2 表为空: {path}')
        rows = list(WowTalentNodeMetadata.objects.filter(
            talent_version=version, tree_type='class',
            source__in=AUTHORITATIVE_TALENT_SOURCES,
        ).values('class_name', 'spec_name', 'talent_id', 'node_id'))
        try:
            specs = derive_granted_entries(tables, rows)
        except (KeyError, ValueError, TypeError) as exc:
            raise CommandError(f'DB2 赠送关系不可用: {exc}') from exc
        identities = {str(spec_id) for spec_id in SPEC_IDENTITY_MAP}
        if set(specs) != identities:
            raise CommandError(f'赠送专精覆盖不完整：缺失 {sorted(identities - set(specs))}，未知 {sorted(set(specs) - identities)}')
        snapshot = {'build': build, 'specs': specs}
        total = sum(map(len, specs.values()))
        self.stdout.write(f'预检通过：build={build} 专精={len(specs)} Entry关系={total} 防战={specs.get("73")}')
        if not options['apply']:
            self.stdout.write('只读预览；需 --apply 才写入。')
            return
        with transaction.atomic():
            locked = WowTalentVersion.objects.select_for_update().get(pk=version.pk)
            if locked.current_build != build:
                raise CommandError('版本 build 在预检期间发生变化，拒绝写入')
            locked.granted_entries_json = snapshot
            locked.save(update_fields=['granted_entries_json'])
        self.stdout.write(self.style.SUCCESS('已写入版本行赠送快照；请独立回查 API/DB。'))
