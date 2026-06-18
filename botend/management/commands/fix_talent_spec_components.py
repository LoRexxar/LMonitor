# -*- coding: utf-8 -*-
"""修复 WowTalentNodeMetadata 中跨专精污染的 spec 组件。

同一职业 TraitTree 会包含多个 spec-side 连通分量。旧 backfill 曾把全部分量写入每个
spec，导致 Blood/Frost/Unholy 等页面混入其它专精节点。本命令从 DB2 + 玩家样本推断
当前 spec 所属 component，补齐 db2_tree_id/db2_component_id，并删除不属于当前 spec 的
spec 节点。
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import WowTalentNodeMetadata
from botend.wow.talents.db2_components import TalentDb2ComponentResolver


class Command(BaseCommand):
    help = '修复天赋元数据 spec 连通分量归属，清理跨专精污染节点'

    def add_arguments(self, parser):
        parser.add_argument('--dump-dir', default='.cache/wago_db2_dumps/latest', help='DB2 dump 目录')
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--spec-name', default='', help='仅处理指定专精')
        parser.add_argument('--dry-run', action='store_true', help='只输出统计，不写入/删除')

    def handle(self, *args, **options):
        dump_dir = options['dump_dir']
        only_class = options['class_name']
        only_spec = options['spec_name']
        dry_run = options['dry_run']

        resolver = TalentDb2ComponentResolver(dump_dir)
        self.stdout.write(f'已加载 DB2 spec components: {sum(len(v) for v in resolver.tree_components.values())}')

        classes = [only_class] if only_class else list(CLASS_SPEC_MAP.keys())
        total_updates = 0
        total_delete = 0

        for class_name in classes:
            specs = CLASS_SPEC_MAP.get(class_name, [])
            if only_spec:
                specs = [s for s in specs if s == only_spec]
            for spec_name in specs:
                updated, deleted = self._fix_spec(resolver, class_name, spec_name, dry_run)
                total_updates += updated
                total_delete += deleted

        self.stdout.write(self.style.SUCCESS(
            f'完成: 补字段 {total_updates} 条, 删除污染 spec 节点 {total_delete} 条'
            + (' (dry-run)' if dry_run else '')
        ))

    def _fix_spec(self, resolver, class_name, spec_name, dry_run):
        qs = WowTalentNodeMetadata.objects.filter(class_name=class_name, spec_name=spec_name)
        to_update = []
        delete_ids = []
        delete_examples = []

        for row in qs.iterator(chunk_size=500):
            trait_node_id = resolver.trait_node_for_entry(row.node_id) or row.talent_id
            if not trait_node_id:
                continue
            info = resolver.db2_nodes.get(trait_node_id)
            if not info:
                continue
            tree_id = info['tree_id']
            component_id = resolver.component_id_for_trait_node(trait_node_id)

            should_update = False
            if row.db2_tree_id != tree_id:
                row.db2_tree_id = tree_id
                should_update = True
            if row.db2_component_id != component_id:
                row.db2_component_id = component_id
                should_update = True

            if (row.tree_type or 'spec') == 'spec':
                target_component_ids = resolver.get_spec_component_ids(class_name, spec_name, tree_id)
                if target_component_ids and component_id not in target_component_ids:
                    delete_ids.append(row.id)
                    if len(delete_examples) < 8:
                        delete_examples.append(
                            f"{row.id}:{row.name_zh or row.name or row.node_id} "
                            f"component={component_id} target={sorted(target_component_ids)}"
                        )
                    continue

            if should_update:
                row.last_updated = timezone.now()
                to_update.append(row)

        if delete_ids:
            self.stdout.write(
                f'{class_name}/{spec_name}: 将删除 {len(delete_ids)} 条污染 spec 节点; 示例: '
                + '; '.join(delete_examples)
            )
        if to_update:
            self.stdout.write(f'{class_name}/{spec_name}: 将补 db2 字段 {len(to_update)} 条')

        if not dry_run:
            if to_update:
                WowTalentNodeMetadata.objects.bulk_update(
                    to_update,
                    ['db2_tree_id', 'db2_component_id', 'last_updated'],
                    batch_size=500,
                )
            if delete_ids:
                WowTalentNodeMetadata.objects.filter(id__in=delete_ids).delete()

        return len(to_update), len(delete_ids)
