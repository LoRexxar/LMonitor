"""下载固定正式服版本的 Wago 表，验证完整性并保存可重放的来源清单。"""
import csv
import hashlib
import html
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from botend.services.download_checkpoint import (PartDownload, atomic_bytes, atomic_json,
                                                read_json, valid_rows)


TABLES = (
    'JournalInstance', 'JournalEncounter', 'JournalEncounterSection', 'JournalEncounterItem',
    'JournalSectionXDifficulty', 'JournalItemXDifficulty', 'JournalTier', 'JournalTierXInstance',
    'JournalEncounterCreature', 'Map', 'MapDifficulty', 'Difficulty', 'Item', 'ItemSparse',
    'ItemSpec', 'ItemSpecOverride', 'ChrSpecialization', 'GemProperties',
    'Spell', 'SpellName', 'SpellEffect', 'SpellMisc', 'SpellDuration', 'SpellRadius',
    'SpellRange', 'SpellAuraOptions', 'SpellTargetRestrictions', 'DungeonEncounter', 'JournalEncounterXDifficulty',
)
LOCALIZED_TABLES = {'JournalInstance', 'JournalEncounter', 'JournalEncounterSection', 'JournalTier',
                    'JournalEncounterCreature', 'Map', 'Difficulty', 'ItemSparse', 'Spell', 'SpellName'}
DOWNLOAD_SLOTS = BoundedSemaphore(12)
logger = logging.getLogger(__name__)


def _maintenance(root):
    from .download_retention import maintain_download_parts
    report = maintain_download_parts(root, apply=True)
    warning = report.get('maintenance_warning') or report.get('blocked')
    if warning:
        logger.warning('Download parts maintenance warning for %s: %s', root, warning)
    return report


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
    def __init__(self, build, directory, *, offline=False, refresh=False, progress=None, max_workers=6,
                 branch='unspecified'):
        if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', build):
            raise ValueError('必须指定完整游戏版本号')
        self.build = build
        self.branch = branch
        self.directory = Path(directory) / build / 'zhCN'
        self.offline = offline
        self.refresh = refresh
        self.progress = progress or (lambda message: None)
        self.max_workers = max(1, min(6, int(max_workers)))
        self.manifest = {}
        self._selected_row_ids = {}
        self._part_downloads = {}

    def identity(self, table, locale):
        return dict(branch=self.branch, build=self.build, table=table, locale=locale)

    def segmented_table(self, table, locale):
        from .download_retention import download_lock
        with download_lock(self.directory / f'{table}.{locale}.parts'):
            return self._segmented_table_locked(table, locale)

    def _segmented_table_locked(self, table, locale):
        """按来源行号取得 ID 边界，分段流式下载，失败仅重试未完成段。"""
        params = {'build': self.build, 'locale': locale, 'sort[ID]': 'asc'}
        with http_session() as session:
            response = session.get(f'https://wago.tools/api/db2-find/{table}', params=params, timeout=(10, 45))
            response.raise_for_status()
            page = response.json()
        total = int(page['total'])
        per_page = int(page['per_page'])
        chunk_size = 5000
        if total <= 0 or per_page <= 0 or chunk_size % per_page:
            raise ValueError(f'{table} 返回无效分页边界')
        directory = self.directory / f'{table}.{locale}.parts'
        checkpoint = PartDownload(directory, self.identity(table, locale), total, chunk_size,
                                  self.refresh, per_page=per_page)
        self._part_downloads[table, locale] = checkpoint

        def segment(offset):
            count = min(chunk_size, total - offset)
            saved = checkpoint.load(offset, count)
            if saved is not None:
                return saved
            with DOWNLOAD_SLOTS, http_session() as session:
                if offset:
                    response = session.get(f'https://wago.tools/api/db2-find/{table}',
                                           params={**params, 'page': offset // per_page + 1}, timeout=(10, 60))
                    response.raise_for_status()
                    position = response.json()
                else:
                    position = page
                if int(position['total']) != total or int(position['per_page']) != per_page:
                    raise ValueError(f'{table} 来源条数或分页布局在下载中变化，请重新同步')
                first_id = int(position['data'][0]['ID'])
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
                    if not valid_rows(rows, count) or int(rows[0]['ID']) != first_id:
                        raise ValueError(f'{table} 分段 {offset} 返回不完整数据')
            checkpoint.save(offset, rows)
            self.progress(f'{table} 分段完成：{offset + count}/{total}')
            return rows
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            groups = list(pool.map(segment, range(0, total, chunk_size)))
        rows = [row for group in groups for row in group]
        if not valid_rows(rows, total):
            raise ValueError(f'{table} 分段合并不完整或包含重复记录')
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode('utf-8')

    def table(self, table, locale='zhCN', *, row_filter=None, required_fields=()):
        from .download_retention import download_lock, maintain_download_parts
        if self.offline or table not in ('ItemSparse', 'SpellEffect', 'SpellMisc'):
            return self._table_locked(table, locale, row_filter=row_filter, required_fields=required_fields)
        root = self.directory / f'{table}.{locale}.parts'
        with download_lock(root):
            self._part_downloads.pop((table, locale), None)
            _maintenance(root)
            result = self._table_locked(table, locale, row_filter=row_filter, required_fields=required_fields)
            _maintenance(root)
            return result

    def _table_locked(self, table, locale='zhCN', *, row_filter=None, required_fields=()):
        path = self.directory / f'{table}{"" if locale == "zhCN" else "." + locale}.csv'
        url = f'https://wago.tools/db2/{table}/csv?build={self.build}&locale={locale}'
        metadata_path = path.with_suffix('.csv.manifest.json')
        saved = read_json(metadata_path) if not self.offline else None
        cached = path.read_bytes() if path.exists() else None
        verified = (isinstance(saved, dict) and saved.get('identity') == self.identity(table, locale)
                    and cached is not None and saved.get('sha256') == hashlib.sha256(cached).hexdigest())
        if self.offline and not path.exists():
            raise ValueError(f'离线数据缺少 {table}')
        if not self.offline and (self.refresh or not verified):
            if table in ('ItemSparse', 'SpellEffect', 'SpellMisc'):
                content = self.segmented_table(table, locale)
            else:
                with http_session() as session:
                    response = session.get(url, timeout=(15, 90))
                    response.raise_for_status()
                    content = response.content
        else:
            content = cached
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
        key = table if locale == 'zhCN' else f'{table}.{locale}'
        metadata = {'rows': total_rows, 'sha256': hashlib.sha256(content).hexdigest(), 'url': url,
                    'identity': self.identity(table, locale), 'headers': reader.fieldnames}
        if not self.offline:
            atomic_bytes(path, content)
            try:
                atomic_json(metadata_path, metadata)
            except BaseException:
                # A metadata failure must not leave a newly replaced CSV next
                # to the old manifest. Each write is atomic; the table lock
                # serializes cooperating writers through this rollback.
                if cached is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_bytes(path, cached)
                raise
            checkpoint = self._part_downloads.get((table, locale))
            if checkpoint is not None:
                checkpoint.complete(metadata)
        self.manifest[key] = metadata
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
        with ThreadPoolExecutor(max_workers=min(3, self.max_workers)) as pool:
            result = dict(zip(table_names, pool.map(
                lambda table: self.table(table, 'zhCN' if table in LOCALIZED_TABLES else 'enUS'), table_names)))
        return result
