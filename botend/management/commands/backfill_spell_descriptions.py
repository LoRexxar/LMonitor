# -*- coding: utf-8 -*-
"""
从本地 DB2 Spell CSV 批量填充天赋描述到 WowSpellSnapshot 和 WowTalentNodeMetadata。
用法: python manage.py backfill_spell_descriptions [--csv PATH] [--dry-run]
"""

import csv
import os

from django.core.management.base import BaseCommand

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata


class Command(BaseCommand):
    help = '从本地 DB2 Spell CSV 批量填充天赋描述'

    def add_arguments(self, parser):
        parser.add_argument('--csv', default='', help='Spell CSV 路径（默认自动检测）')
        parser.add_argument('--dry-run', action='store_true', help='仅打印不写入')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        csv_path = options['csv']

        # 自动检测 CSV 路径
        if not csv_path:
            candidates = [
                '.cache/wago_db2_dumps/latest/Spell_zhCN.csv',
                '.cache/wago_db2_dumps/latest/Spell_enUS.csv',
            ]
            for c in candidates:
                if os.path.exists(c):
                    csv_path = c
                    break

        if not csv_path or not os.path.exists(csv_path):
            self.stdout.write(self.style.ERROR(f'找不到 Spell CSV 文件'))
            self.stdout.write('请先运行: python manage.py dump_wago_db2_tables --tables Spell --locale zhCN')
            return

        self.stdout.write(f'读取 CSV: {csv_path}')

        # 读取 CSV，构建 spell_id -> description 映射
        desc_map = {}
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    sid = int(row.get('ID') or 0)
                except (ValueError, TypeError):
                    continue
                if sid <= 0:
                    continue
                desc = row.get('Description_lang') or ''
                aura = row.get('AuraDescription_lang') or ''
                if desc or aura:
                    desc_map[sid] = (desc, aura)

        self.stdout.write(f'CSV 中有 {len(desc_map)} 个有描述的法术')

        # 获取所有有 spell_id 的天赋节点
        all_spell_ids = set(
            WowTalentNodeMetadata.objects.exclude(spell_id__isnull=True)
            .values_list('spell_id', flat=True).distinct()
        )
        self.stdout.write(f'数据库中有 {len(all_spell_ids)} 个唯一 spell_id')

        # 找出需要更新的
        need_update = {sid for sid in all_spell_ids if sid in desc_map}
        self.stdout.write(f'其中 {len(need_update)} 个在 CSV 中有描述数据')

        if dry_run:
            for sid in list(need_update)[:10]:
                desc, aura = desc_map[sid]
                self.stdout.write(f'  spell {sid}: {desc[:60]}...' if desc else f'  spell {sid}: (aura only)')
            return

        # 更新 WowSpellSnapshot
        updated_snapshots = 0
        for sid in need_update:
            desc, aura = desc_map[sid]
            snap = WowSpellSnapshot.objects.filter(spell_id=sid).order_by('-updated_at').first()
            if snap:
                changed = False
                if desc and not snap.description:
                    snap.description = desc
                    changed = True
                if aura and not snap.aura_description:
                    snap.aura_description = aura
                    changed = True
                if changed:
                    snap.save(update_fields=['description', 'aura_description', 'updated_at'])
                    updated_snapshots += 1
            else:
                WowSpellSnapshot.objects.create(
                    spell_id=sid,
                    description=desc,
                    aura_description=aura,
                )
                updated_snapshots += 1

        self.stdout.write(f'更新了 {updated_snapshots} 条 WowSpellSnapshot')

        # 更新 WowTalentNodeMetadata.description
        updated_metadata = 0
        for sid in need_update:
            desc, aura = desc_map[sid]
            if desc:
                count = WowTalentNodeMetadata.objects.filter(
                    spell_id=sid, description=''
                ).update(description=desc)
                updated_metadata += count

        self.stdout.write(f'更新了 {updated_metadata} 条 WowTalentNodeMetadata.description')

        # 检查覆盖率
        total_meta = WowTalentNodeMetadata.objects.exclude(spell_id__isnull=True).count()
        with_desc = WowTalentNodeMetadata.objects.exclude(description='').count()
        self.stdout.write(f'描述覆盖率: {with_desc}/{total_meta} ({100*with_desc/total_meta:.1f}%)')

        self.stdout.write(self.style.SUCCESS(
            f'完成: {updated_snapshots} snapshots, {updated_metadata} metadata'
        ))
