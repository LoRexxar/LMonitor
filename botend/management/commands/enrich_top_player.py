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
            cur.execute("""
                UPDATE wow_spec_top_player tp
                JOIN wow_spec_dungeon_ranking dr ON (
                    dr.character_name = tp.character_name
                    AND dr.class_name = tp.class_name
                    AND dr.spec_name = tp.spec_name
                )
                SET tp.talents_json = dr.talents_json,
                    tp.gear_json = dr.gear_json
                WHERE tp.talents_json NOT LIKE '%%"talentID"%%'
                   OR tp.gear_json = '[]'
            """)
            dungeon_updated = cur.rowcount
            self.stdout.write(f"  从 dungeon_ranking 更新: {dungeon_updated} 条")

            # 3. 从 raid_ranking 匹配更新（补充 dungeon 没覆盖的）
            cur.execute("""
                UPDATE wow_spec_top_player tp
                JOIN wow_spec_raid_ranking rr ON (
                    rr.character_name = tp.character_name
                    AND rr.class_name = tp.class_name
                    AND rr.spec_name = tp.spec_name
                )
                SET tp.talents_json = rr.talents_json,
                    tp.gear_json = rr.gear_json
                WHERE tp.talents_json NOT LIKE '%%"talentID"%%'
                   OR tp.gear_json = '[]'
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
                WHERE gear_json != '[]' AND gear_json IS NOT NULL AND gear_json != ''
            """)
            has_gear = cur.fetchone()[0]

            self.stdout.write(self.style.SUCCESS(
                f"\n完成！结构化天赋: {already_structured} → {final_structured}/{total}, "
                f"有装备: {has_gear}/{total}"
            ))
