# -*- coding: utf-8 -*-

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = '合并 WoW 天赋元数据中同节点的跨 tree_type 重复行（SQL 批量版）'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--spec-name', default='', help='仅处理指定专精')

    def handle(self, *args, **options):
        class_name = (options.get('class_name') or '').strip()
        spec_name = (options.get('spec_name') or '').strip()

        # 两套 where：单表查询用 bare，多表 join 用 a. 前缀
        bare_parts = ["spell_id IS NOT NULL"]
        a_parts = ["a.spell_id IS NOT NULL"]
        params = []
        if class_name:
            bare_parts.append("class_name = %s")
            a_parts.append("a.class_name = %s")
            params.append(class_name)
        if spec_name:
            bare_parts.append("spec_name = %s")
            a_parts.append("a.spec_name = %s")
            params.append(spec_name)
        bare_where = " AND ".join(bare_parts)
        a_where = " AND ".join(a_parts)

        total_deleted = 0
        id_col = "COALESCE(node_id, talent_id, spell_id)"

        with connection.cursor() as c:
            # 步骤1：删除 spec 行（已有对应 class 行）
            c.execute(f"""
                DELETE a FROM wow_talent_node_metadata a
                INNER JOIN wow_talent_node_metadata b
                  ON a.class_name = b.class_name
                  AND a.spec_name = b.spec_name
                  AND {id_col.replace('node_id', 'a.node_id').replace('talent_id', 'a.talent_id').replace('spell_id', 'a.spell_id')}
                   = {id_col.replace('node_id', 'b.node_id').replace('talent_id', 'b.talent_id').replace('spell_id', 'b.spell_id')}
                  AND b.tree_type = 'class'
                  AND a.tree_type = 'spec'
                WHERE {a_where}
            """, params)
            d1 = c.rowcount
            total_deleted += d1

            # 步骤2：删除 spec 行（已有对应 hero 行）
            c.execute(f"""
                DELETE a FROM wow_talent_node_metadata a
                INNER JOIN wow_talent_node_metadata b
                  ON a.class_name = b.class_name
                  AND a.spec_name = b.spec_name
                  AND {id_col.replace('node_id', 'a.node_id').replace('talent_id', 'a.talent_id').replace('spell_id', 'a.spell_id')}
                   = {id_col.replace('node_id', 'b.node_id').replace('talent_id', 'b.talent_id').replace('spell_id', 'b.spell_id')}
                  AND b.tree_type = 'hero'
                  AND a.tree_type = 'spec'
                WHERE {a_where}
            """, params)
            d2 = c.rowcount
            total_deleted += d2

            # 步骤3：删除 hero 行（已有对应 class 行）
            c.execute(f"""
                DELETE a FROM wow_talent_node_metadata a
                INNER JOIN wow_talent_node_metadata b
                  ON a.class_name = b.class_name
                  AND a.spec_name = b.spec_name
                  AND {id_col.replace('node_id', 'a.node_id').replace('talent_id', 'a.talent_id').replace('spell_id', 'a.spell_id')}
                   = {id_col.replace('node_id', 'b.node_id').replace('talent_id', 'b.talent_id').replace('spell_id', 'b.spell_id')}
                  AND b.tree_type = 'class'
                  AND a.tree_type = 'hero'
                WHERE {a_where}
            """, params)
            d3 = c.rowcount
            total_deleted += d3

            # 步骤4：删除同 tree_type 的多余行（保留 id 最大的）
            c.execute(f"""
                DELETE a FROM wow_talent_node_metadata a
                INNER JOIN wow_talent_node_metadata b
                  ON a.class_name = b.class_name
                  AND a.spec_name = b.spec_name
                  AND a.tree_type = b.tree_type
                  AND {id_col.replace('node_id', 'a.node_id').replace('talent_id', 'a.talent_id').replace('spell_id', 'a.spell_id')}
                   = {id_col.replace('node_id', 'b.node_id').replace('talent_id', 'b.talent_id').replace('spell_id', 'b.spell_id')}
                  AND a.id < b.id
                WHERE {a_where}
            """, params)
            d4 = c.rowcount
            total_deleted += d4

            # 检查剩余重复
            c.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT class_name, spec_name, {id_col} AS identity
                    FROM wow_talent_node_metadata
                    WHERE {bare_where}
                    GROUP BY class_name, spec_name, {id_col}
                    HAVING COUNT(*) > 1
                ) t
            """, params)
            remaining = c.fetchone()[0]

        self.stdout.write(
            f'spec(已有class): {d1}, spec(已有hero): {d2}, '
            f'hero(已有class): {d3}, 同类型多余: {d4}'
        )
        self.stdout.write(self.style.SUCCESS(
            f'已删除 {total_deleted} 条重复行，剩余 {remaining} 组'
        ))
