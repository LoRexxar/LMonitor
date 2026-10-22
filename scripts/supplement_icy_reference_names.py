"""为攻略中既有 Wago 词表未覆盖的引用补充按编号核验的中文名称。"""

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from scripts.build_icy_full_bundle import dump, load_terms


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    folder = args.work_dir
    terms = load_terms(folder)
    existing = {(row['kind'], row['object_id']): row for row in terms}
    references = {(ref['kind'], ref['id']): ref['source_name']
                  for path in (folder / 'converted').glob('*.json')
                  for ref in json.loads(path.read_text(encoding='utf-8'))['check_data']['source_refs'].values()}
    missing = {key: name for key, name in references.items() if key not in existing}
    local = threading.local()

    def fetch(pair):
        kind, object_id = pair
        path = folder / 'name-lookups' / f'{kind}-{object_id}.json'
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        if not hasattr(local, 'session'):
            local.session = requests.Session()
        url = f'https://nether.wowhead.com/tooltip/{kind}/{object_id}?locale=4'
        response = local.session.get(url, timeout=(10, 30))
        if response.status_code == 404:
            result = {'identity': pair, 'status': '来源编号无可用中文名称', 'url': url}
        else:
            response.raise_for_status()
            payload = response.json()
            name = payload.get('name', '')
            if not re.search(r'[\u3400-\u9fff]', name):
                result = {'identity': pair, 'status': '该编号尚未提供中文名称', 'url': url}
            else:
                result = {'term': {'game_version': '12.1.5', 'kind': kind, 'object_id': object_id,
                                   'name_en': missing[pair], 'name_zh': name, 'icon': payload.get('icon', ''),
                                   'evidence': 'Wago 本地快照/既有词表未覆盖；按 Icy Veins 原文编号核验 Wowhead 中文名称：' + url}}
        dump(path, result)
        return result

    found, unresolved, errors = [], [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch, pair): pair for pair in missing}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
                if 'term' in result:
                    found.append(result['term'])
                else:
                    unresolved.append(result)
            except Exception as exc:
                errors.append({'identity': futures[future], 'error': str(exc)})
            if number % 100 == 0 or number == len(futures):
                print(json.dumps({'完成': number, '总数': len(futures), '确认中文': len(found), '请求异常': len(errors)}, ensure_ascii=False), flush=True)
    existing.update({(row['kind'], row['object_id']): row for row in found})
    dump(folder / 'authoritative-terms.json', list(existing.values()))
    dump(folder / 'reference-name-lookups.json', {'terms': found, 'unresolved': unresolved, 'errors': errors})


if __name__ == '__main__':
    main()
