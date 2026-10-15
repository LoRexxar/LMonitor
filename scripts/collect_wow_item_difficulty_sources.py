"""Read-only public exact-build DB2 collector, including all pages."""
import concurrent.futures
import json
from pathlib import Path
import requests

BUILD = '12.1.0.69587'
AUDIT = Path('/home/ubuntu/journal-difficulty-audit')
TASKS = [('ItemCreationContext', 'ItemCreationContextGroupID', k) for k in (6, 7, 8, 9)]
TASKS += [('ItemScalingConfig', 'ID', k) for k in (183, 184, 211)]
TASKS += [('ItemOffsetCurve', 'ID', 47), ('Curve', 'ID', 88583), ('CurvePoint', 'CurveID', 88583), ('ItemSquishEra', 'ID', 2)]
TASKS += [('ItemBonusTreeNode', 'ParentItemBonusTreeID', k) for k in (51, 5246, 5523, 5902, 5903)]
TASKS += [('ItemBonus', 'ParentItemBonusListID', k) for k in (13577, 13578, 13534, 6652, 12667, 40, 41, 42, 43)]
TASKS += [('ItemBonusListGroup', 'ID', k) for k in (608, 609)]

def collect(task):
    table, field, key = task
    url = f'https://wago.tools/api/db2-find/{table}?build={BUILD}&filter[{field}]=exact:{key}&locale=enUS'
    rows = []
    page = 1
    while True:
        response = requests.get(url + f'&page={page}', timeout=30)
        response.raise_for_status()
        data = response.json()
        rows.extend(data['data'])
        if not data['next_page_url']:
            break
        page += 1
    assert len(rows) == data['total']
    assert len({r['ID'] for r in rows}) == len(rows)
    assert all(r[field] == key for r in rows)
    envelope = dict(build=BUILD, table=table, filter={field: key}, total=len(rows), rows=rows, url=url)
    AUDIT.mkdir(parents=True, exist_ok=True)
    (AUDIT / f'{table}-{field}-{key}.json').write_text(json.dumps(envelope, indent=2))
    print(table, key, len(rows))

if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(collect, TASKS))
