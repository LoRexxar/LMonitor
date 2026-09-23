"""Reconcile existing talent icons from exact-build DB2 entry identities."""
import csv
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.wow.talents.icon_source import resolve_talent_entry_icon
from botend.wow.talents.metadata import AUTHORITATIVE_TALENT_SOURCES


def _rows(path):
    with path.open(newline='', encoding='utf-8-sig') as source:
        yield from csv.DictReader(source)


class Command(BaseCommand):
    help = '对照同 build DB2 的 Entry 图标；默认只读，--apply 才更新'

    def add_arguments(self, parser):
        parser.add_argument('--version-key', required=True)
        parser.add_argument('--build', required=True)
        parser.add_argument('--dump-dir', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        version = WowTalentVersion.objects.get(key=options['version_key'])
        build = options['build'].strip()
        root = Path(options['dump_dir'])
        if not build or version.current_build != build or root.name != build:
            raise CommandError('版本 current_build、--build 与 dump-dir 末级必须一致')
        needed = ('TraitNodeEntry', 'TraitNodeXTraitNodeEntry', 'TraitDefinition_enUS',
                  'SpellMisc', 'file_data_icon_cache')
        for name in needed:
            if not (root / (name + '.csv')).is_file():
                raise CommandError(f'缺少同 build DB2 资料：{name}.csv')

        nodes = list(WowTalentNodeMetadata.objects.filter(
            talent_version=version, source__in=AUTHORITATIVE_TALENT_SOURCES,
        ).values('id', 'node_id', 'talent_id', 'spell_id', 'display_spell_id',
                 'icon', 'class_name', 'spec_name', 'tree_type'))
        entries = {int(r['ID']): int(r['TraitDefinitionID'])
                   for r in _rows(root / 'TraitNodeEntry.csv')}
        physical = {int(r['TraitNodeEntryID']): int(r['TraitNodeID'])
                    for r in _rows(root / 'TraitNodeXTraitNodeEntry.csv')}
        definitions = {int(r['ID']): r for r in _rows(root / 'TraitDefinition_enUS.csv')}
        spell_ids = {int(v or 0) for n in nodes for v in (n['spell_id'], n['display_spell_id'])}
        spell_ids.update(int(d.get(field) or 0) for d in definitions.values()
                         for field in ('SpellID', 'VisibleSpellID', 'OverridesSpellID'))
        spell_misc = {}
        for r in _rows(root / 'SpellMisc.csv'):
            sid = int(r['SpellID'] or 0)
            if sid in spell_ids:
                if sid in spell_misc and (
                    r.get('SpellIconFileDataID'), r.get('ActiveIconFileDataID')
                ) != (
                    spell_misc[sid].get('SpellIconFileDataID'),
                    spell_misc[sid].get('ActiveIconFileDataID'),
                ):
                    raise CommandError(f'SpellID {sid} 存在不同图标的难度变体，需先按 DifficultyID 核查')
                spell_misc[sid] = r
        icon_names = {int(r['FileDataID']): r['IconName'] for r in _rows(root / 'file_data_icon_cache.csv')
                      if r.get('FileDataID') and r.get('IconName')}

        unresolved = Counter()
        changes = []
        per_class = Counter()
        for node in nodes:
            entry_id = int(node['node_id'] or 0)
            definition_id = entries.get(entry_id)
            if not definition_id:
                if node['tree_type'] != 'hero_anchor':
                    unresolved['no_definition'] += 1
                continue
            if physical.get(entry_id) != int(node['talent_id'] or 0):
                raise CommandError(f'Entry {entry_id} 与站内 TraitNode.ID 不一致，拒绝变更')
            file_id, icon = resolve_talent_entry_icon(entry_id, entries, definitions, spell_misc, icon_names)
            if not file_id:
                unresolved['no_icon_id'] += 1
                continue
            if not icon:
                unresolved['unmapped_file_id'] += 1
                continue
            if icon != (node['icon'] or ''):
                changes.append((node, icon, file_id))
                per_class[node['class_name']] += 1
        self.stdout.write(f'版本={version.key} build={build} 扫描={len(nodes)} 待修改={len(changes)} 未解={dict(unresolved)}')
        self.stdout.write(f'职业分布={dict(sorted(per_class.items()))}')
        for node, icon, file_id in changes[:30]:
            self.stdout.write(f"  {node['class_name']}/{node['spec_name']} Entry={node['node_id']} "
                              f"FileDataID={file_id} {node['icon']!r} -> {icon!r}")
        if not options['apply']:
            self.stdout.write('只读预检，未写数据库。')
            return
        with transaction.atomic():
            locked_version = WowTalentVersion.objects.select_for_update().get(pk=version.pk)
            if locked_version.current_build != build:
                raise CommandError('写入前 build 变化，拒绝更新')
            by_id = {n.pk: n for n in WowTalentNodeMetadata.objects.select_for_update().filter(
                pk__in=[node['id'] for node, _, _ in changes]
            )}
            updates = []
            for original, icon, _ in changes:
                current = by_id.get(original['id'])
                if current is None or current.icon != original['icon'] or current.node_id != original['node_id']:
                    raise CommandError(f"Entry {original['node_id']} 在预检期间变化，拒绝覆盖")
                current.icon = icon
                updates.append(current)
            if updates:
                WowTalentNodeMetadata.objects.bulk_update(updates, ['icon'], batch_size=200)
        self.stdout.write(self.style.SUCCESS(f'已更新 {len(changes)} 行；须独立回读 DB/API/CDN。'))
