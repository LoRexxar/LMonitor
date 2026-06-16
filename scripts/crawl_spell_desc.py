"""快速爬取 Spell 表中天赋相关的描述"""
import requests, re, json, html as html_mod, csv, os, time, sys

os.environ['DJANGO_SETTINGS_MODULE'] = 'LMonitor.settings_dev'
sys.path.insert(0, '/home/ubuntu/LMonitor')
import django; django.setup()
from django.db import connection

DUMP_DIR = '.cache/wago_db2_dumps/latest'
os.makedirs(DUMP_DIR, exist_ok=True)

# 获取需要的 spell_id
cursor = connection.cursor()
cursor.execute('SELECT DISTINCT spell_id FROM wow_talent_node_metadata WHERE spell_id IS NOT NULL')
needed = set(int(r[0]) for r in cursor.fetchall())

# 加上 TraitDefinition 的关联 spell_id
for fname in ['TraitDefinition_zhCN.csv', 'TraitDefinition_enUS.csv', 'TraitDefinition.csv']:
    fpath = os.path.join(DUMP_DIR, fname)
    if not os.path.exists(fpath):
        continue
    with open(fpath) as f:
        for row in csv.DictReader(f):
            for field in ['VisibleSpellID', 'OverridesSpellID', 'SpellID']:
                val = row.get(field, '')
                if val and val != '0':
                    try:
                        needed.add(int(val))
                    except:
                        pass

print(f'target spell_ids: {len(needed)}', flush=True)

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

PATTERN = re.compile(r'data-page="([^"]+)"')

matched = {}
page = 1
total_pages = 16095
start = time.time()

while page <= total_pages:
    url = f'https://wago.tools/db2/Spell?build=12.0.5.67823&locale=zhCN&page={page}'
    for attempt in range(4):
        try:
            r = session.get(url, timeout=20)
            break
        except Exception:
            if attempt >= 3:
                print(f'FATAL: page {page} failed after 4 attempts', flush=True)
                raise
            time.sleep(1)

    m = PATTERN.search(r.text or '')
    if not m:
        break
    raw = m.group(1)
    obj = json.loads(html_mod.unescape(raw))
    data = (obj.get('props') or {}).get('data') or {}
    rows = data.get('data') or []
    if not rows:
        break

    for row in rows:
        sid = int(row.get('ID', 0) or 0)
        if sid in needed:
            desc = (row.get('Description_lang') or '').strip()
            aura = (row.get('AuraDescription_lang') or '').strip()
            if desc or aura:
                matched[sid] = {'desc': desc, 'aura': aura}

    if page % 200 == 0:
        elapsed = time.time() - start
        rate = page / max(elapsed, 1)
        eta = (total_pages - page) / max(rate, 1)
        print(f'page {page}/{total_pages}, matched: {len(matched)}, {rate:.0f}p/s, ETA: {eta/60:.1f}min', flush=True)

    page += 1
    time.sleep(0.005)

elapsed = time.time() - start
print(f'\nDone! {len(matched)} matches in {elapsed/60:.1f}min', flush=True)

out_path = os.path.join(DUMP_DIR, 'Spell_zhCN_talent.json')
with open(out_path, 'w') as f:
    json.dump(matched, f, ensure_ascii=False)
print(f'Saved to {out_path}', flush=True)
