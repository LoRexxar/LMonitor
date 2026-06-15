# -*- coding: utf-8 -*-

import json
import os
import time
import html
import re

import requests
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '分页批量抓取 wago.tools DB2 表到本地 jsonl（第一步：只爬取，不写 MySQL）'

    def add_arguments(self, parser):
        parser.add_argument('--build', default='', help='指定 build（不填则使用当前默认值）')
        parser.add_argument('--locale', default='enUS', help='locale，例如 enUS')
        parser.add_argument(
            '--tables',
            default='TraitNode,TraitNodeEntry,TraitDefinition,TraitNodeXTraitNodeEntry,TraitEdge,SpellName,SpellMisc',
            help='逗号分隔表名',
        )
        parser.add_argument('--output-dir', default='.cache/wago_db2_dumps', help='输出目录（会按 build 分目录）')
        parser.add_argument('--sleep', type=float, default=0.05, help='每页请求间隔秒数，防止限流')
        parser.add_argument('--max-pages', type=int, default=0, help='最多抓取页数（0 不限制）')
        parser.add_argument('--overwrite', action='store_true', help='覆盖已有 jsonl/meta（重新从第 1 页抓取）')
        parser.add_argument('--retry', type=int, default=6, help='单页网络重试次数')
        parser.add_argument('--retry-sleep', type=float, default=2.0, help='重试基础等待秒数（指数退避）')
        parser.add_argument('--no-proxy', action='store_true', help='不使用系统代理（默认使用环境代理）')
        parser.add_argument('--resume', action='store_true', help='若存在 progress 文件则从上次页码继续')

    def handle(self, *args, **options):
        build = (options.get('build') or '').strip() or 'latest'
        locale = (options.get('locale') or 'enUS').strip() or 'enUS'
        tables = [t.strip() for t in (options.get('tables') or '').split(',') if t.strip()]
        out_root = (options.get('output_dir') or '.cache/wago_db2_dumps').strip() or '.cache/wago_db2_dumps'
        sleep = float(options.get('sleep') or 0)
        max_pages = int(options.get('max_pages') or 0)
        overwrite = bool(options.get('overwrite'))
        retry = max(0, int(options.get('retry') or 0))
        retry_sleep = max(0.1, float(options.get('retry_sleep') or 2.0))
        no_proxy = bool(options.get('no_proxy'))
        resume = bool(options.get('resume'))

        out_dir = os.path.join(out_root, build)
        os.makedirs(out_dir, exist_ok=True)

        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        # 注意：该环境直连可能超时，默认启用环境代理；如需强制直连用 --no-proxy
        session.trust_env = not no_proxy

        self.stdout.write(f'输出目录: {out_dir}')
        for table in tables:
            self._dump_table(
                session,
                out_dir,
                table,
                build,
                locale,
                sleep=sleep,
                max_pages=max_pages,
                overwrite=overwrite,
                retry=retry,
                retry_sleep=retry_sleep,
                resume=resume,
            )

    def _dump_table(self, session, out_dir, table, build, locale, sleep=0.0, max_pages=0, overwrite=False, retry=6, retry_sleep=2.0, resume=False):
        file_path = os.path.join(out_dir, f'{table}.jsonl')
        meta_path = os.path.join(out_dir, f'{table}.meta.json')
        progress_path = os.path.join(out_dir, f'{table}.progress.json')

        self.stdout.write(f'开始抓取 {table} ...')
        total_rows = 0
        page = 1
        last_page = None

        if overwrite:
            if os.path.exists(file_path):
                os.remove(file_path)
            if os.path.exists(meta_path):
                os.remove(meta_path)
            if os.path.exists(progress_path):
                os.remove(progress_path)

        if resume and os.path.exists(progress_path) and not overwrite:
            try:
                with open(progress_path, 'r', encoding='utf-8') as f:
                    progress = json.load(f) or {}
                page = int(progress.get('page') or 1)
                if page < 1:
                    page = 1
                self.stdout.write(f'{table} resume from page={page}')
            except Exception:
                page = 1

        with open(file_path, 'a', encoding='utf-8') as f:
            while True:
                url = f'https://wago.tools/db2/{table}?build={build}&locale={locale}&page={page}'
                r = None
                for attempt in range(retry + 1):
                    try:
                        r = session.get(url, timeout=60)
                    except Exception as exc:
                        if attempt >= retry:
                            self.stdout.write(self.style.ERROR(f'{table} page={page} 网络失败: {exc}'))
                            raise
                        time.sleep(retry_sleep * (2 ** attempt))
                        continue
                    if r.status_code == 200:
                        break
                    if attempt >= retry:
                        self.stdout.write(self.style.ERROR(f'{table} page={page} status={r.status_code}'))
                        raise RuntimeError(f'{table} page={page} status={r.status_code}')
                    time.sleep(retry_sleep * (2 ** attempt))

                props = self._extract_inertia_props(r.text or '')
                payload = props.get('data') or {}
                rows = payload.get('data') or []
                if last_page is None:
                    try:
                        last_page = int(payload.get('last_page') or 1)
                    except Exception:
                        last_page = 1
                    try:
                        total = int(payload.get('total') or 0)
                    except Exception:
                        total = 0
                    self.stdout.write(f'{table} total={total} last_page={last_page}')

                if not isinstance(rows, list) or not rows:
                    break
                for row in rows:
                    if isinstance(row, dict):
                        f.write(json.dumps(row, ensure_ascii=False) + '\n')
                        total_rows += 1

                if page % 20 == 0:
                    self.stdout.write(f'{table} progress page={page}/{last_page} rows={total_rows}')
                try:
                    with open(progress_path, 'w', encoding='utf-8') as pf:
                        pf.write(json.dumps({'table': table, 'page': page, 'rows': total_rows}, ensure_ascii=False) + '\n')
                except Exception:
                    pass
                if max_pages and page >= max_pages:
                    break
                if last_page and page >= last_page:
                    break
                page += 1
                if sleep > 0:
                    time.sleep(sleep)

        meta = {
            'table': table,
            'build': build,
            'locale': locale,
            'rows': total_rows,
            'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        with open(meta_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(meta, ensure_ascii=False, indent=2) + '\n')
        self.stdout.write(self.style.SUCCESS(f'完成 {table}: rows={total_rows} -> {file_path}'))

    @staticmethod
    def _extract_inertia_props(html_text):
        m = re.search(r'data-page=(?:"([^"]+)"|\'([^\']+)\')', html_text or '')
        if not m:
            return {}
        raw = m.group(1) or m.group(2) or ''
        try:
            obj = json.loads(html.unescape(raw))
        except Exception:
            return {}
        return obj.get('props') or {}
