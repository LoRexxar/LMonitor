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
            default='TraitNode,TraitNodeEntry,TraitDefinition,TraitNodeXTraitNodeEntry,TraitEdge',
            help='逗号分隔表名',
        )
        parser.add_argument('--output-dir', default='.cache/wago_db2_dumps', help='输出目录（会按 build 分目录）')
        parser.add_argument('--sleep', type=float, default=0.05, help='每页请求间隔秒数，防止限流')
        parser.add_argument('--max-pages', type=int, default=0, help='最多抓取页数（0 不限制）')

    def handle(self, *args, **options):
        build = (options.get('build') or '').strip() or 'latest'
        locale = (options.get('locale') or 'enUS').strip() or 'enUS'
        tables = [t.strip() for t in (options.get('tables') or '').split(',') if t.strip()]
        out_root = (options.get('output_dir') or '.cache/wago_db2_dumps').strip() or '.cache/wago_db2_dumps'
        sleep = float(options.get('sleep') or 0)
        max_pages = int(options.get('max_pages') or 0)

        out_dir = os.path.join(out_root, build)
        os.makedirs(out_dir, exist_ok=True)

        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})

        self.stdout.write(f'输出目录: {out_dir}')
        for table in tables:
            self._dump_table(session, out_dir, table, build, locale, sleep=sleep, max_pages=max_pages)

    def _dump_table(self, session, out_dir, table, build, locale, sleep=0.0, max_pages=0):
        file_path = os.path.join(out_dir, f'{table}.jsonl')
        meta_path = os.path.join(out_dir, f'{table}.meta.json')

        self.stdout.write(f'开始抓取 {table} ...')
        total_rows = 0
        page = 1
        last_page = None

        with open(file_path, 'w', encoding='utf-8') as f:
            while True:
                url = f'https://wago.tools/db2/{table}?build={build}&locale={locale}&page={page}'
                r = session.get(url, timeout=60)
                if r.status_code != 200:
                    self.stdout.write(self.style.ERROR(f'{table} page={page} status={r.status_code}'))
                    break
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
