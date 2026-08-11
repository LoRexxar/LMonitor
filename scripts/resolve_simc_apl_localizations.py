#!/usr/bin/env python
"""搜索 Wowhead 并生成可离线应用的 SimC APL 中文本地化覆盖包。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) '
    'Gecko/20100101 Firefox/128.0'
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description='按 APL 英文 token 搜索 Wowhead，并固化官方或推断中文。',
    )
    parser.add_argument('--input', required=True, help='阶段 42 的完整 APL 字段 JSON')
    parser.add_argument('--output', required=True, help='本地化覆盖包输出 JSON')
    parser.add_argument(
        '--search-cache',
        default=str(PROJECT_ROOT / '.cache' / 'simc-apl-wowhead-name-search.json'),
        help='Wowhead 名称搜索缓存',
    )
    parser.add_argument(
        '--translation-cache',
        default=str(PROJECT_ROOT / '.cache' / 'simc-apl-token-translation-zh.json'),
        help='内部 token 机器语义缓存',
    )
    parser.add_argument(
        '--spell-cache',
        default=str(PROJECT_ROOT / '.cache' / 'simc-apl-wowhead-zh.json'),
        help='阶段 42 已有的 Spell ID 简中缓存',
    )
    parser.add_argument('--fetch-wowhead', action='store_true', help='抓取缺失的 Wowhead 搜索和 tooltip')
    parser.add_argument('--fetch-wowhead-search', action='store_true', help='仅抓取缺失的 Wowhead 名称搜索')
    parser.add_argument('--fetch-wowhead-tooltip', action='store_true', help='仅抓取缺失的 Wowhead 简中 tooltip')
    parser.add_argument('--fetch-translation', action='store_true', help='翻译 Wowhead 无结果的内部英文语义')
    parser.add_argument('--refresh-failed', action='store_true', help='重试搜索缓存中的失败项')
    parser.add_argument('--workers', type=int, default=4, help='网络并发数')
    parser.add_argument('--delay', type=float, default=0.08, help='每次请求前延迟秒数')
    parser.add_argument('--max-searches', type=int, default=0, help='仅调试：限制本轮新增搜索数，0 为不限')
    parser.add_argument(
        '--settings',
        default=os.environ.get('DJANGO_SETTINGS_MODULE', 'LMonitor.settings'),
        help='Django settings 模块',
    )
    return parser.parse_args()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.2 * (attempt + 1))


def load_json(path, default):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'无法读取 JSON {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise RuntimeError(f'JSON 顶层必须为对象: {path}')
    return value


def request_with_retries(url, *, params, accept_language, delay, retries=2):
    last_error = ''
    for attempt in range(retries + 1):
        try:
            if delay:
                time.sleep(delay)
            response = requests.get(
                url, params=params, timeout=(8, 35),
                headers={
                    'User-Agent': USER_AGENT,
                    'Accept': 'application/json,text/html,text/plain,*/*',
                    'Accept-Language': accept_language,
                },
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = re.sub(r'\s+', ' ', f'{type(exc).__name__}: {exc}')[:500]
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(last_error or f'请求失败: {url}')


def fetch_search(query, *, delay, parse_search):
    try:
        response = request_with_retries(
            'https://www.wowhead.com/search', params={'q': query},
            accept_language='en-US,en;q=0.9', delay=delay,
        )
        candidates = parse_search(response.text)
        expected = re.sub(r'[^a-z0-9]+', '', query.casefold())
        exact = [
            row for row in candidates
            if re.sub(r'[^a-z0-9]+', '', row['name_en'].casefold()) == expected
        ][:30]
        return query, {
            'status': 'ok' if exact else 'no_exact_match',
            'query': query,
            'search_url': response.url,
            'candidates': exact,
            'fetched_at': utc_now(),
        }
    except (RuntimeError, TypeError, ValueError) as exc:
        return query, {
            'status': 'request_failed', 'query': query, 'candidates': [],
            'error': str(exc)[:500], 'fetched_at': utc_now(),
        }


def fetch_tooltip(spell_id, *, delay):
    try:
        response = request_with_retries(
            f'https://nether.wowhead.com/tooltip/spell/{spell_id}',
            params={'dataEnv': 1, 'locale': 4},
            accept_language='zh-CN,zh;q=0.9,en;q=0.7', delay=delay,
        )
        payload = response.json()
        raw_name = str(payload.get('name') or '').strip() if isinstance(payload, dict) else ''
        return str(spell_id), {
            'status': 'ok' if CJK_RE.search(raw_name) else ('unlocalized' if raw_name else 'empty'),
            'name_zh': raw_name if CJK_RE.search(raw_name) else '',
            'raw_name': raw_name,
            'fetched_at': utc_now(),
        }
    except (RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return str(spell_id), {
            'status': 'request_failed', 'name_zh': '', 'raw_name': '',
            'error': str(exc)[:500], 'fetched_at': utc_now(),
        }


def fetch_translation(phrase, *, delay):
    try:
        response = request_with_retries(
            'https://translate.googleapis.com/translate_a/single',
            params={'client': 'gtx', 'sl': 'en', 'tl': 'zh-CN', 'dt': 't', 'q': phrase},
            accept_language='zh-CN,zh;q=0.9', delay=delay,
        )
        payload = response.json()
        translated = ''.join(
            str(part[0] or '') for part in (payload[0] if isinstance(payload, list) and payload else [])
            if isinstance(part, list) and part
        ).strip()
        if not CJK_RE.search(translated):
            translated = ''
        return phrase, {
            'status': 'ok' if translated else 'untranslated',
            'name_zh': translated,
            'fetched_at': utc_now(),
        }
    except (RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return phrase, {
            'status': 'request_failed', 'name_zh': '',
            'error': str(exc)[:500], 'fetched_at': utc_now(),
        }


def fetch_batch(keys, worker, *, workers, cache_path, cache_payload, section, label):
    keys = list(keys)
    if not keys:
        return
    cache = cache_payload[section]
    stats = Counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(worker, key): key for key in keys}
        for index, future in enumerate(as_completed(futures), start=1):
            key, record = future.result()
            cache[key] = record
            stats[record.get('status') or 'unknown'] += 1
            if index % 20 == 0 or index == len(futures):
                cache_payload['updated_at'] = utc_now()
                atomic_write_json(cache_path, cache_payload)
                summary = ', '.join(f'{key}={value}' for key, value in sorted(stats.items()))
                print(f'{label}进度 {index}/{len(futures)}: {summary}', flush=True)


def main():
    args = parse_arguments()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.environ['DJANGO_SETTINGS_MODULE'] = args.settings

    import django

    django.setup()

    from botend.services.simc_apl.localization_enrichment import (
        build_localization_overrides,
        parse_wowhead_search_html,
        required_wowhead_queries,
    )
    from botend.services.simc_apl.metadata_package import build_metadata_package

    source = load_json(args.input, {})
    package = build_metadata_package(source)

    search_path = Path(args.search_cache).resolve()
    search_payload = load_json(search_path, {
        'schema_version': 1, 'data_env': 1, 'locale': 4,
        'records': {}, 'tooltips': {},
    })
    if (search_payload.get('schema_version') != 1 or search_payload.get('data_env') != 1 or
            search_payload.get('locale') != 4 or not isinstance(search_payload.get('records'), dict) or
            not isinstance(search_payload.get('tooltips'), dict)):
        raise RuntimeError('Wowhead 搜索缓存 schema/data_env/locale 无效')
    search_records = search_payload['records']

    required_queries = required_wowhead_queries(package)
    retryable = {'request_failed'} if args.refresh_failed else set()
    missing_queries = [
        query for query in required_queries
        if query not in search_records or search_records[query].get('status') in retryable
    ]
    if args.max_searches > 0:
        missing_queries = missing_queries[:args.max_searches]
    print(
        f'Wowhead 名称搜索: required={len(required_queries)} '
        f'cached={len(required_queries) - len(missing_queries)} missing={len(missing_queries)}',
        flush=True,
    )
    if (args.fetch_wowhead or args.fetch_wowhead_search) and missing_queries:
        fetch_batch(
            missing_queries,
            lambda query: fetch_search(
                query, delay=max(0.0, args.delay), parse_search=parse_wowhead_search_html,
            ),
            workers=args.workers, cache_path=search_path,
            cache_payload=search_payload, section='records', label='Wowhead 搜索',
        )

    stage42 = load_json(args.spell_cache, {'records': {}})
    stage42_records = stage42.get('records') if isinstance(stage42.get('records'), dict) else {}
    tooltips = search_payload['tooltips']
    for spell_id, row in stage42_records.items():
        if spell_id not in tooltips and isinstance(row, dict):
            tooltips[spell_id] = {
                key: row.get(key) for key in ('status', 'name_zh', 'raw_name', 'fetched_at')
            }
    candidate_ids = sorted({
        int(candidate['spell_id'])
        for record in search_records.values() if isinstance(record, dict)
        for candidate in (record.get('candidates') or []) if isinstance(candidate, dict)
        if isinstance(candidate.get('spell_id'), int) and candidate['spell_id'] > 0
    })
    tooltip_retryable = {'request_failed'} if args.refresh_failed else set()
    missing_ids = [
        spell_id for spell_id in candidate_ids
        if str(spell_id) not in tooltips
        or tooltips[str(spell_id)].get('status') in tooltip_retryable
    ]
    print(
        f'Wowhead 中文 tooltip: candidates={len(candidate_ids)} '
        f'cached={len(candidate_ids) - len(missing_ids)} missing={len(missing_ids)}',
        flush=True,
    )
    if (args.fetch_wowhead or args.fetch_wowhead_tooltip) and missing_ids:
        fetch_batch(
            missing_ids,
            lambda spell_id: fetch_tooltip(spell_id, delay=max(0.0, args.delay)),
            workers=args.workers, cache_path=search_path,
            cache_payload=search_payload, section='tooltips', label='Wowhead tooltip',
        )
    for record in search_records.values():
        if not isinstance(record, dict):
            continue
        for candidate in record.get('candidates') or []:
            localized = tooltips.get(str(candidate.get('spell_id'))) or {}
            candidate['name_zh'] = str(localized.get('name_zh') or '')
            candidate['tooltip_status'] = str(localized.get('status') or 'not_fetched')
    search_payload['updated_at'] = utc_now()
    atomic_write_json(search_path, search_payload)

    translation_path = Path(args.translation_cache).resolve()
    translation_payload = load_json(translation_path, {'schema_version': 1, 'records': {}})
    if translation_payload.get('schema_version') != 1 or not isinstance(translation_payload.get('records'), dict):
        raise RuntimeError('机器语义缓存 schema 无效')
    translations = translation_payload['records']
    provisional = build_localization_overrides(
        package, search_cache=search_payload, translation_cache=translation_payload,
    )
    phrases = sorted({
        row['evidence']['query'] for row in provisional['records']
        if row['localization_source'] == 'token_fallback'
    })
    missing_phrases = [phrase for phrase in phrases if phrase not in translations]
    print(
        f'内部语义翻译: required={len(phrases)} '
        f'cached={len(phrases) - len(missing_phrases)} missing={len(missing_phrases)}',
        flush=True,
    )
    if args.fetch_translation and missing_phrases:
        fetch_batch(
            missing_phrases,
            lambda phrase: fetch_translation(phrase, delay=max(0.0, args.delay)),
            workers=args.workers, cache_path=translation_path,
            cache_payload=translation_payload, section='records', label='语义翻译',
        )
    translation_payload['updated_at'] = utc_now()
    atomic_write_json(translation_path, translation_payload)

    overrides = build_localization_overrides(
        package, search_cache=search_payload, translation_cache=translation_payload,
    )
    atomic_write_json(args.output, overrides)
    print(
        f"本地化覆盖已生成: output={Path(args.output).resolve()} "
        f"records={overrides['counts']['record_count']} "
        f"blank={overrides['counts']['blank_count']} "
        f"sources={overrides['counts']['source_counts']}",
        flush=True,
    )


if __name__ == '__main__':
    main()
