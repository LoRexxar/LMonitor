"""
从 DB2 TraitDefinition 爬取全量天赋描述并写入数据库。

流程：
1. 爬取 TraitDefinition (enUS + zhCN) 全量数据
2. 通过 TraitNodeEntry → TraitNodeXTraitNodeEntry → TraitNode 映射
3. 匹配数据库 node_id (= TraitNodeEntry.ID)
4. 批量更新 description / description_zh
"""
import csv
import json
import os
import re
import time
import html as html_mod

import requests
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = '从 DB2 TraitDefinition 爬取全量天赋描述并写入数据库'

    def add_arguments(self, parser):
        parser.add_argument('--dump-dir', default='.cache/wago_db2_dumps/latest')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--sleep', type=float, default=0.05)
        parser.add_argument('--skip-crawl', action='store_true', help='跳过爬取，用已有本地数据')

    def handle(self, *args, **options):
        dump_dir = options['dump_dir']
        batch_size = options['batch_size']
        sleep = options['sleep']
        skip_crawl = options['skip_crawl']

        os.makedirs(dump_dir, exist_ok=True)

        # ===== 1. 爬取 TraitDefinition =====
        for locale in ['enUS', 'zhCN']:
            csv_path = os.path.join(dump_dir, f'TraitDefinition_{locale}.csv')
            if skip_crawl and os.path.exists(csv_path):
                self.stdout.write(f'跳过爬取 {locale}（已有本地数据）')
                continue
            self._crawl_trait_definition(locale, csv_path, sleep)

        # ===== 2. 加载映射 =====
        self.stdout.write('加载映射数据...')

        # TraitNodeXTraitNodeEntry: entry_id → node_id
        entry_to_node = {}
        with open(os.path.join(dump_dir, 'TraitNodeXTraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                eid = int(row['TraitNodeEntryID'])
                nid = int(row['TraitNodeID'])
                if nid > 0:
                    entry_to_node[eid] = nid

        # TraitNode: node_id → tree_id (用于过滤有效节点)
        db2_nodes = {}
        with open(os.path.join(dump_dir, 'TraitNode.csv')) as f:
            for row in csv.DictReader(f):
                nid = int(row['ID'])
                db2_nodes[nid] = {
                    'tree_id': int(row['TraitTreeID']),
                    'pos_y': int(row['PosY']) if row['PosY'] else 0,
                }

        # TraitNodeEntry: entry_id → def_id
        entry_to_def = {}
        with open(os.path.join(dump_dir, 'TraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                entry_to_def[int(row['ID'])] = int(row['TraitDefinitionID'])

        # ===== 3. 加载 TraitDefinition 描述 =====
        self.stdout.write('加载 TraitDefinition 描述...')

        def _load_defs(locale):
            path = os.path.join(dump_dir, f'TraitDefinition_{locale}.csv')
            if not os.path.exists(path):
                # 回退到原始文件
                path = os.path.join(dump_dir, 'TraitDefinition.csv')
            defs = {}
            with open(path) as f:
                for row in csv.DictReader(f):
                    did = int(row['ID'])
                    name = row.get('OverrideName_lang', '') or ''
                    desc = row.get('OverrideDescription_lang', '') or ''
                    defs[did] = {'name': name, 'desc': desc}
                return defs

        defs_en = _load_defs('enUS')
        defs_zh = _load_defs('zhCN')
        self.stdout.write(f'  enUS: {len(defs_en)} entries, {sum(1 for d in defs_en.values() if d["desc"])} has desc')
        self.stdout.write(f'  zhCN: {len(defs_zh)} entries, {sum(1 for d in defs_zh.values() if d["desc"])} has desc')

        # ===== 4. 构建 node_id → 描述 映射 =====
        self.stdout.write('构建 node_id → 描述映射...')

        # 数据库中的 node_id = TraitNodeEntry.ID
        # 映射链: node_id(=entry_id) → entry_to_def → def_id → defs_en/defs_zh
        cursor = connection.cursor()
        cursor.execute('SELECT id, node_id FROM wow_talent_node_metadata WHERE node_id IS NOT NULL')
        db_rows = cursor.fetchall()

        updates = []
        for db_id, node_id in db_rows:
            # entry_id = node_id
            def_id = entry_to_def.get(node_id)
            if not def_id:
                continue

            en = defs_en.get(def_id, {})
            zh = defs_zh.get(def_id, {})

            desc_en = en.get('desc', '').strip()
            desc_zh = zh.get('desc', '').strip()
            name_en = en.get('name', '').strip()
            name_zh = zh.get('name', '').strip()

            if not desc_en and not desc_zh:
                continue

            updates.append({
                'db_id': db_id,
                'description': desc_en,
                'description_zh': desc_zh,
                'name_en': name_en,
                'name_zh': name_zh,
            })

        self.stdout.write(f'匹配到描述: {len(updates)} / {len(db_rows)}')

        # ===== 5. 批量写入 =====
        self.stdout.write('批量写入数据库...')

        total = len(updates)
        written = 0

        for i in range(0, total, batch_size):
            batch = updates[i:i + batch_size]

            ids_str = ','.join(str(u['db_id']) for u in batch)
            desc_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN %s" for u in batch
            )
            desc_zh_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN %s" for u in batch
            )
            name_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN %s" for u in batch
            )
            name_zh_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN %s" for u in batch
            )

            desc_params = [u['description'] for u in batch]
            desc_zh_params = [u['description_zh'] for u in batch]
            name_params = [u['name_en'] for u in batch]
            name_zh_params = [u['name_zh'] for u in batch]

            sql = f'''
                UPDATE wow_talent_node_metadata
                SET description = CASE {desc_cases} END,
                    description_zh = CASE {desc_zh_cases} END,
                    name = CASE WHEN name IS NULL OR name = '' THEN CASE {name_cases} END ELSE name END,
                    name_zh = CASE WHEN name_zh IS NULL OR name_zh = '' THEN CASE {name_zh_cases} END ELSE name_zh END
                WHERE id IN ({ids_str})
            '''
            cursor.execute(sql, desc_params + desc_zh_params + name_params + name_zh_params)
            written += len(batch)

            if (i // batch_size) % 10 == 0:
                self.stdout.write(f'  进度: {written}/{total}')

        # ===== 6. 验证 =====
        cursor.execute("SELECT COUNT(*) FROM wow_talent_node_metadata WHERE description IS NOT NULL AND description != ''")
        en_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM wow_talent_node_metadata WHERE description_zh IS NOT NULL AND description_zh != ''")
        zh_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM wow_talent_node_metadata")
        total_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM wow_talent_node_metadata WHERE name IS NOT NULL AND name != ''")
        name_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM wow_talent_node_metadata WHERE name_zh IS NOT NULL AND name_zh != ''")
        name_zh_count = cursor.fetchone()[0]

        self.stdout.write(self.style.SUCCESS(
            f'\n完成！已写入 {written} 条。'
            f'\n  description(EN): {en_count} / {total_count} ({en_count*100/total_count:.1f}%)'
            f'\n  description_zh(CN): {zh_count} / {total_count} ({zh_count*100/total_count:.1f}%)'
            f'\n  name(EN): {name_count} / {total_count} ({name_count*100/total_count:.1f}%)'
            f'\n  name_zh(CN): {name_zh_count} / {total_count} ({name_zh_count*100/total_count:.1f}%)'
        ))

    def _crawl_trait_definition(self, locale, csv_path, sleep):
        self.stdout.write(f'爬取 TraitDefinition locale={locale} ...')
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        all_rows = []
        page = 1
        while True:
            url = f'https://wago.tools/db2/TraitDefinition?build=12.0.5.67823&locale={locale}&page={page}'
            for attempt in range(6):
                try:
                    r = session.get(url, timeout=30)
                    break
                except Exception:
                    if attempt >= 5:
                        raise
                    time.sleep(2 ** attempt)

            m = re.search(r'data-page=(?:"([^"]+)"|' r"'([^']+)')", r.text or '')
            if not m:
                break
            raw = m.group(1) or m.group(2) or ''
            obj = json.loads(html_mod.unescape(raw))
            data = (obj.get('props') or {}).get('data') or {}
            rows = data.get('data') or []
            if not rows:
                break

            all_rows.extend(rows)
            last_page = int(data.get('last_page') or 1)
            if page % 10 == 0:
                self.stdout.write(f'  page {page}/{last_page} ({len(all_rows)} rows)')
            if page >= last_page:
                break
            page += 1
            if sleep > 0:
                time.sleep(sleep)

        # 保存
        fieldnames = sorted(set().union(*(r.keys() for r in all_rows)))
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in all_rows:
                writer.writerow(row)

        self.stdout.write(f'  完成 {locale}: {len(all_rows)} rows -> {csv_path}')
