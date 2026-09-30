"""下载基希克斯的官方中文文本及手册依赖，生成可离线导入的制品。"""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
BUILD = '12.1.5.70077'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'botend/data/kithix_localization_12_1_5.json')
    args = parser.parse_args()
    cache = ROOT / '.cache' / 'kithix-localization' / BUILD
    cache.mkdir(parents=True, exist_ok=True)
    evidence = []

    def fetch(table, field=None, value=None, locale='zhCN'):
        if table not in {'ItemSparse', 'Spell', 'SpellName', 'JournalInstance', 'JournalEncounter',
                         'JournalEncounterSection', 'JournalEncounterCreature', 'JournalTier', 'Map', 'Difficulty'}:
            locale = 'enUS'
        params = {'build': BUILD, 'locale': locale}
        if field:
            params[f'filter[{field}]'] = f'exact:{value}'
        key = hashlib.sha256(json.dumps([table, params], sort_keys=True).encode()).hexdigest()
        path = cache / f'{key}.json'
        if path.exists():
            saved = json.loads(path.read_text(encoding='utf-8'))
        else:
            session = requests.Session()
            session.trust_env = False
            session.mount('https://', HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5)))
            rows = []
            if not field:
                response = session.get(f'https://wago.tools/db2/{table}/csv', params=params, timeout=(10, 90))
                response.raise_for_status()
                rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
            page = 1
            while field:
                response = session.get(f'https://wago.tools/api/db2-find/{table}',
                                       params={**params, 'page': page}, timeout=(10, 40))
                response.raise_for_status()
                data = response.json()
                rows.extend(data['data'])
                if page >= data['last_page']:
                    if len(rows) != data['total']:
                        raise ValueError(f'{table} 下载不完整')
                    break
                page += 1
            if field and any(str(row.get(field)) != str(value) for row in rows):
                raise ValueError(f'{table} 返回了筛选范围外的数据')
            saved = {'url': response.url, 'rows': rows}
            path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        evidence.append({'table': table, 'url': saved['url'], 'rows': len(saved['rows'])})
        return saved['rows']

    def many(table, field, ids, locale='zhCN'):
        with ThreadPoolExecutor(max_workers=8) as pool:
            groups = list(pool.map(lambda value: fetch(table, field, value, locale), sorted(ids)))
        return [row for group in groups for row in group]

    tables = {}
    for table, field, value in (
        ('JournalInstance', 'ID', 1324), ('JournalEncounter', 'JournalInstanceID', 1324),
        ('JournalEncounterSection', 'JournalEncounterID', 2896),
        ('JournalEncounterItem', 'JournalEncounterID', 2896),
        ('JournalEncounterCreature', 'JournalEncounterID', 2896),
        ('JournalTierXInstance', 'JournalInstanceID', 1324), ('Map', 'ID', 3095),
        ('MapDifficulty', 'MapID', 3095), ('DungeonEncounter', 'ID', 3513),
        ('JournalEncounterXDifficulty', 'JournalEncounterID', 2896),
    ):
        tables[table] = fetch(table, field, value)
        print(f'{table}：{len(tables[table])} 条', flush=True)
    for table in ('JournalTier', 'Difficulty', 'SpellDuration', 'SpellRadius', 'SpellRange'):
        tables[table] = fetch(table)
        print(f'{table}：{len(tables[table])} 条', flush=True)
    for table, field, source in (
        ('JournalSectionXDifficulty', 'JournalEncounterSectionID', 'JournalEncounterSection'),
        ('JournalItemXDifficulty', 'JournalEncounterItemID', 'JournalEncounterItem'),
    ):
        ids = {int(row['ID']) for row in tables[source]}
        tables[table] = [row for row in fetch(table) if int(row[field]) in ids]
    item_ids = {row['ItemID'] for row in tables['JournalEncounterItem']}
    for table in ('ItemSparse', 'Item'):
        tables[table] = many(table, 'ID', item_ids)
    original = json.loads((ROOT / 'botend/data/ptr_kithix_unbound_12_1_5.json').read_text(encoding='utf-8'))
    effect_ids = {effect['spell_id'] for item in original['gear']['items']
                  for variant in item['variants'] for effect in variant['effects']}
    needed = effect_ids | {row['SpellID'] for row in tables['JournalEncounterSection']} - {0}
    reference = re.compile(r'\$@(?:spellname|spelldesc|spellaura|spelltooltip)(\d+)|\$(\d+)(?:[sSmMaAtTdDuUiIrR]|proccooldown)')
    queried = set()
    tables['Spell'] = []
    while pending := needed - queried:
        queried.update(pending)
        rows = many('Spell', 'ID', pending)
        tables['Spell'].extend(rows)
        for row in rows + tables['JournalEncounterSection']:
            needed.update(int(left or right) for left, right in reference.findall(json.dumps(row)))
        print(f'已下载 {len(queried)} 个技能依赖', flush=True)
    tables['SpellName'] = many('SpellName', 'ID', needed)
    for table in ('SpellEffect', 'SpellMisc', 'SpellAuraOptions', 'SpellTargetRestrictions'):
        tables[table] = many(table, 'SpellID', needed)
    payload = {'schema': 1, 'instance_id': 1324, 'encounter_id': 2896,
               'localization_build': BUILD, 'locale': 'zhCN', 'tables': tables,
               'english_effects': many('Spell', 'ID', effect_ids, 'enUS'),
               'sources': sorted(evidence, key=lambda row: row['url'])}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'中文制品已保存：{args.output}')


if __name__ == '__main__':
    main()
