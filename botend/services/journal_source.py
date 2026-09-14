"""下载固定正式服版本的 Wago 表，验证完整性并保存可重放的来源清单。"""
import csv
import hashlib
import html
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


TABLES = (
    'JournalInstance', 'JournalEncounter', 'JournalEncounterSection', 'JournalEncounterItem',
    'JournalSectionXDifficulty', 'JournalItemXDifficulty', 'JournalTier', 'JournalTierXInstance',
    'JournalEncounterCreature', 'Map', 'MapDifficulty', 'Difficulty', 'Item', 'ItemSparse',
    'Spell', 'SpellName', 'SpellEffect', 'SpellMisc', 'SpellDuration', 'SpellRadius',
    'SpellRange', 'SpellAuraOptions', 'SpellTargetRestrictions', 'DungeonEncounter', 'JournalEncounterXDifficulty',
)
LOCALIZED_TABLES = {'JournalInstance', 'JournalEncounter', 'JournalEncounterSection', 'JournalTier',
                    'JournalEncounterCreature', 'Map', 'Difficulty', 'ItemSparse', 'Spell', 'SpellName'}
DOWNLOAD_SLOTS = BoundedSemaphore(12)


def http_session():
    session = requests.Session()
    # 批量 CSV 经本机系统代理会长时间卡住；直连下载不改动系统代理设置。
    session.trust_env = False
    session.headers['User-Agent'] = 'LMonitor-AdventureJournal/1.0'
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    return session


def inertia(text):
    match = re.search(r'data-page="([^"]+)"', text)
    if not match:
        raise ValueError('Wago 未返回可识别的数据页面')
    return json.loads(html.unescape(match.group(1)))['props']


def latest_retail_build():
    with http_session() as session:
        response = session.get('https://wago.tools/builds', params={'product': 'wow'}, timeout=(10, 60))
        response.raise_for_status()
        rows = inertia(response.text)['builds']['data']
    builds = [r['version'] for r in rows if r.get('product') == 'wow']
    if not builds:
        raise ValueError('未找到正式服版本；不会使用 PTR 默认版本')
    return max(builds, key=lambda b: tuple(map(int, b.split('.'))))


class WagoJournalSource:
    def __init__(self, build, directory, *, offline=False, refresh=False, progress=None):
        if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', build):
            raise ValueError('必须指定完整游戏版本号')
        self.build = build
        self.directory = Path(directory) / build / 'zhCN'
        self.offline = offline
        self.refresh = refresh
        self.progress = progress or (lambda message: None)
        self.manifest = {}
        self._selected_row_ids = {}

    def segmented_table(self, table, locale):
        """按来源行号取得 ID 边界，分段流式下载，失败仅重试未完成段。"""
        params = {'build': self.build, 'locale': locale, 'sort[ID]': 'asc'}
        with http_session() as session:
            response = session.get(f'https://wago.tools/api/db2-find/{table}', params=params, timeout=(10, 45))
            response.raise_for_status()
            page = response.json()
        total = int(page['total'])
        per_page = int(page['per_page'])
        chunk_size = 5000
        directory = self.directory / f'{table}.{locale}.parts'
        directory.mkdir(parents=True, exist_ok=True)

        def segment(offset):
            count = min(chunk_size, total - offset)
            path = directory / f'{offset}-{total}.json'
            if path.exists() and not self.refresh:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if len(saved) == count:
                    return saved
            with DOWNLOAD_SLOTS, http_session() as session:
                if offset:
                    response = session.get(f'https://wago.tools/api/db2-find/{table}',
                                           params={**params, 'page': offset // per_page + 1}, timeout=(10, 60))
                    response.raise_for_status()
                    position = response.json()
                else:
                    position = page
                first_id = int(position['data'][0]['ID'])
                if int(position['total']) != total:
                    raise ValueError(f'{table} 来源条数在下载中变化，请重新同步')
                with session.get(f'https://wago.tools/db2/{table}/csv',
                                 params={**params, 'filter[ID]': f'>{first_id - 1}'},
                                 stream=True, timeout=(10, 60)) as response:
                    response.raise_for_status()
                    lines = (line.decode('utf-8-sig') + '\n' for line in response.iter_lines())
                    reader = csv.DictReader(lines)
                    rows = []
                    for row in reader:
                        if len(rows) == count:
                            break
                        rows.append(row)
                    if len(rows) != count or int(rows[0]['ID']) != first_id:
                        raise ValueError(f'{table} 分段 {offset} 返回不完整数据')
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
            self.progress(f'{table} 分段完成：{offset + count}/{total}')
            return rows
        with ThreadPoolExecutor(max_workers=6) as pool:
            groups = list(pool.map(segment, range(0, total, chunk_size)))
        rows = [row for group in groups for row in group]
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode('utf-8')

    def table(self, table, locale='zhCN', *, row_filter=None, required_fields=()):
        path = self.directory / f'{table}{"" if locale == "zhCN" else "." + locale}.csv'
        url = f'https://wago.tools/db2/{table}/csv?build={self.build}&locale={locale}'
        if self.offline and not path.exists():
            raise ValueError(f'离线数据缺少 {table}')
        if not self.offline and (self.refresh or not path.exists()):
            if table in ('ItemSparse', 'SpellEffect', 'SpellMisc'):
                content = self.segmented_table(table, locale)
            else:
                with http_session() as session:
                    response = session.get(url, timeout=(15, 90))
                    response.raise_for_status()
                    content = response.content
        else:
            content = path.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
        missing_fields = {'ID', *required_fields} - set(reader.fieldnames or ())
        if missing_fields:
            missing = '、'.join(sorted(missing_fields))
            raise ValueError(f'{table} 缺少投影字段 {missing}')
        rows = []
        seen_ids = set()
        total_rows = 0
        for row in reader:
            if 'ID' not in row or None in row or None in row.values():
                raise ValueError(f'{table} 返回空表或不完整 CSV，拒绝发布')
            row_id = row['ID']
            if row_id in seen_ids:
                raise ValueError(f'{table} 包含重复记录')
            seen_ids.add(row_id)
            total_rows += 1
            if row_filter is None or row_filter(row):
                rows.append(row)
        if not total_rows:
            raise ValueError(f'{table} 返回空表或不完整 CSV，拒绝发布')
        # 联机下载额外比较页面声明总数，阻止截断但语法仍合法的 CSV。
        if not self.offline:
            with http_session() as session:
                response = session.get(f'https://wago.tools/db2/{table}',
                                       params={'build': self.build, 'locale': locale}, timeout=(10, 90))
                response.raise_for_status()
                props = inertia(response.text)
            if props.get('currentVersion') != self.build or int(props['data']['total']) != total_rows:
                raise ValueError(f'{table} 的版本或完整条数校验不通过')
        if not self.offline:
            self.directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        key = table if locale == 'zhCN' else f'{table}.{locale}'
        self.manifest[key] = {
            'rows': total_rows, 'sha256': hashlib.sha256(content).hexdigest(), 'url': url}
        if row_filter is not None:
            self.manifest[key]['selected_rows'] = len(rows)
        suffix = f'（选中 {len(rows)} 条）' if row_filter is not None else ''
        self.progress(f'{table}：{total_rows} 条{suffix}')
        return rows

    def select(self, table, values, *, field='ID', locale=None):
        """完整校验来源表，但只保留业务引用的行，避免大表常驻内存。"""
        values = {str(value) for value in values}
        locale = locale or ('zhCN' if table in LOCALIZED_TABLES else 'enUS')
        rows = self.table(
            table,
            locale,
            row_filter=lambda row: row[field] in values,
            required_fields=(field,),
        )
        key = table if locale == 'zhCN' else f'{table}.{locale}'
        selected = self._selected_row_ids.setdefault(key, set())
        selected.update(row['ID'] for row in rows)
        self.manifest[key]['selected_rows'] = len(selected)
        return rows

    def load(self, table_names=TABLES):
        table_names = tuple(table_names)
        unknown = set(table_names) - set(TABLES)
        if unknown:
            raise ValueError(f'未知冒险手册表：{", ".join(sorted(unknown))}')
        with ThreadPoolExecutor(max_workers=3) as pool:
            result = dict(zip(table_names, pool.map(
                lambda table: self.table(table, 'zhCN' if table in LOCALIZED_TABLES else 'enUS'), table_names)))
        return result
