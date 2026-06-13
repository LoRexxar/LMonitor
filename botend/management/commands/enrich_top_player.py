# -*- coding: utf-8 -*-
"""
从 dungeon_ranking / raid_ranking 表中匹配 top_player 记录，
用 WCL 的结构化天赋+装备数据替换 Blizz 编码和空装备。
"""

from django.core.management.base import BaseCommand
from django.db import connection

from utils.log import logger


class Command(BaseCommand):
    help = '用排名表的结构化数据补充 top_player 的天赋和装备'

    def handle(self, *args, **options):
        with connection.cursor() as cur:
            # 1. 统计当前状态
            cur.execute("""
                SELECT COUNT(*) FROM wow_spec_top_player
                WHERE talents_json LIKE '%%"talentID"%%'
            """)
            already_structured = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM wow_spec_top_player")
            total = cur.fetchone()[0]

            self.stdout.write(f"top_player 总数: {total}, 已有结构化天赋: {already_structured}")

            # 2. 从 dungeon_ranking 匹配更新（优先，数据最全）
            #    用子查询避免 MySQL UPDATE JOIN 限制
            cur.execute("""
                UPDATE wow_spec_top_player tp
                SET tp.talents_json = (
                    SELECT dr.talents_json FROM wow_spec_dungeon_ranking dr
                    WHERE dr.character_name = tp.character_name
                      AND dr.class_name = tp.class_name
                      AND dr.spec_name = tp.spec_name
                      AND dr.talents_json IS NOT NULL
                      AND dr.talents_json != '[]'
                    LIMIT 1
                ),
                tp.gear_json = (
                    SELECT dr.gear_json FROM wow_spec_dungeon_ranking dr
                    WHERE dr.character_name = tp.character_name
                      AND dr.class_name = tp.class_name
                      AND dr.spec_name = tp.spec_name
                      AND dr.gear_json IS NOT NULL
                      AND dr.gear_json != '[]'
                    LIMIT 1
                )
                WHERE (tp.talents_json NOT LIKE '%%"talentID"%%' OR tp.gear_json = '[]')
                  AND EXISTS (
                    SELECT 1 FROM wow_spec_dungeon_ranking dr2
                    WHERE dr2.character_name = tp.character_name
                      AND dr2.class_name = tp.class_name
                      AND dr2.spec_name = tp.spec_name
                  )
            """)
            dungeon_updated = cur.rowcount
            self.stdout.write(f"  从 dungeon_ranking 更新: {dungeon_updated} 条")

            # 3. 从 raid_ranking 补充（dungeon 没覆盖的）
            cur.execute("""
                UPDATE wow_spec_top_player tp
                SET tp.talents_json = (
                    SELECT rr.talents_json FROM wow_spec_raid_ranking rr
                    WHERE rr.character_name = tp.character_name
                      AND rr.class_name = tp.class_name
                      AND rr.spec_name = tp.spec_name
                      AND rr.talents_json IS NOT NULL
                      AND rr.talents_json != '[]'
                    LIMIT 1
                ),
                tp.gear_json = (
                    SELECT rr.gear_json FROM wow_spec_raid_ranking rr
                    WHERE rr.character_name = tp.character_name
                      AND rr.class_name = tp.class_name
                      AND rr.spec_name = tp.spec_name
                      AND rr.gear_json IS NOT NULL
                      AND rr.gear_json != '[]'
                    LIMIT 1
                )
                WHERE (tp.talents_json NOT LIKE '%%"talentID"%%' OR tp.gear_json = '[]')
                  AND EXISTS (
                    SELECT 1 FROM wow_spec_raid_ranking rr2
                    WHERE rr2.character_name = tp.character_name
                      AND rr2.class_name = tp.class_name
                      AND rr2.spec_name = tp.spec_name
                  )
            """)
            raid_updated = cur.rowcount
            self.stdout.write(f"  从 raid_ranking 补充: {raid_updated} 条")

            # 4. 最终统计
            cur.execute("""
                SELECT COUNT(*) FROM wow_spec_top_player
                WHERE talents_json LIKE '%%"talentID"%%'
            """)
            final_structured = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*) FROM wow_spec_top_player
                WHERE gear_json IS NOT NULL AND gear_json != '[]'
            """)
            has_gear = cur.fetchone()[0]

            self.stdout.write(self.style.SUCCESS(
                f"\n完成！结构化天赋: {already_structured} → {final_structured}/{total}, "
                f"有装备: {has_gear}/{total}"
            ))
