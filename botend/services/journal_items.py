"""Wago 中文缺项使用历史中文名称及 Wowhead 补齐；数值仍来自本批版本。"""
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from botend.services.journal_source import WagoJournalSource, http_session
from botend.services.journal_text import index, integer


def legacy_token_names(item_ids, source):
    """正式服已移除的旧兑换物，仅从同 ID 的官方怀旧服数据补中文名称。"""
    build = '5.5.4.69155'
    path = source.directory / 'legacy-token-names.json'
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    missing = set(item_ids) - {int(k) for k in saved}
    if missing and not source.offline:
        def fetch(iid):
            with http_session() as session:
                response = session.get('https://wago.tools/api/db2-find/ItemSparse',
                                       params={'build': build, 'locale': 'zhCN', 'filter[ID]': f'exact:{iid}'}, timeout=(5, 30))
                response.raise_for_status()
                rows = response.json()['data']
            row = next((r for r in rows if integer(r['ID']) == iid), {})
            return iid, row.get('Display_lang')
        with ThreadPoolExecutor(max_workers=4) as pool:
            for iid, name in pool.map(fetch, sorted(missing)):
                if name:
                    saved[str(iid)] = name
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
    return {int(k): {'name': v, 'source': f'wago-localization-{build}'} for k, v in saved.items() if int(k) in item_ids}


def supplement_items(tables, source, *, enabled=True):
    needed = {integer(r['ItemID']) for r in tables['JournalEncounterItem'] if not integer(r['Flags']) & 1}
    current = index(tables['ItemSparse'])
    missing = needed - {iid for iid, row in current.items() if row.get('Display_lang')}
    if not missing or not enabled:
        return {}
    # 英文表负责同版本完整属性；中文缺口单独保留来源，不混用旧版属性。
    english = index(source.table('ItemSparse', locale='enUS'))
    for iid in missing:
        if iid in english:
            current[iid] = {**english[iid], 'Display_lang': '', 'Description_lang': ''}
    tables['ItemSparse'] = list(current.values())
    supplements = {}
    cache_path = source.directory / 'item-supplements.json'
    if cache_path.exists():
        supplements = {int(k): v for k, v in json.loads(cache_path.read_text(encoding='utf-8')).items()}
        if source.refresh:
            supplements = {k: v for k, v in supplements.items() if v.get('source') != 'wago-enUS'}
    missing -= set(supplements)
    # 已实测完整的中文表，仅复用物品名称。新物品和改名可由当前表/补充数据覆盖。
    if missing:
        legacy = WagoJournalSource('12.0.5.67823', source.directory.parents[1],
                                    offline=source.offline, progress=source.progress)
        names = legacy.table('ItemSparse')
        for row in names:
            iid = integer(row['ID'])
            if iid in missing and row.get('Display_lang'):
                supplements[iid] = {'name': row['Display_lang'], 'source': 'wago-localization-12.0.5.67823'}
        source.manifest['ItemSparse.names.zhCN'] = legacy.manifest['ItemSparse']
        missing -= set(supplements)
    retired = {iid for iid in missing if not english.get(iid, {}).get('Display_lang')}
    if retired:
        supplements.update(legacy_token_names(retired, source))
        missing -= set(supplements)
    # 先保存 Wago 补充成果；第三方不可访问也不能丢失已取得的中文名称。
    if not source.offline:
        cache_path.write_text(json.dumps(supplements, ensure_ascii=False), encoding='utf-8')
    if missing and not source.offline:
        source.progress(f'Wowhead 补充 {len(missing)} 件中文物品信息')
        unavailable = Event()
        def fetch(iid):
            if unavailable.is_set():
                return iid, None
            try:
                with http_session() as session:
                    response = session.get(f'https://nether.wowhead.com/tooltip/item/{iid}',
                                           params={'locale': 'zhcn'}, timeout=(5, 15))
                    if response.status_code in (403, 429):
                        unavailable.set()
                    response.raise_for_status()
                    row = response.json()
            except Exception:
                return iid, None
            if not row.get('name'):
                return iid, None
            return iid, {'name': row['name'], 'quality': row.get('quality', 0),
                         'source': 'wowhead', 'url': response.url}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for count, (iid, row) in enumerate(pool.map(fetch, sorted(missing)), 1):
                if row:
                    supplements[iid] = row
                if count % 25 == 0:
                    cache_path.write_text(json.dumps(supplements, ensure_ascii=False), encoding='utf-8')
                    source.progress(f'中文物品补充：{count}/{len(missing)}')
    for iid in missing - set(supplements):
        if english.get(iid, {}).get('Display_lang'):
            supplements[iid] = {'name': english[iid]['Display_lang'], 'source': 'wago-enUS'}
    if not source.offline:
        cache_path.write_text(json.dumps(supplements, ensure_ascii=False), encoding='utf-8')
    source.manifest['ItemSparse.supplements'] = {
        'rows': len(supplements), 'sha256': hashlib.sha256(json.dumps(supplements, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        'sources': sorted({r['source'] for r in supplements.values()})}
    return supplements
