"""为本地 Wago/既有词表未覆盖的宏技能名，按来源编号核验中文名称。"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from scripts.build_icy_full_bundle import dump, load_terms
from botend.services.class_guide_icy import localize_macro_blocks


def normalized(value):
    return re.sub(r'\((?:racial|summon|teleport)\)$', '', value.casefold().replace('’', "'")).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    folder = args.work_dir
    terms = load_terms(folder)
    records = [json.loads(path.read_text(encoding='utf-8')) for path in (folder / 'converted').glob('*.json')]
    placeholders = {'ability_name', 'trinket', 'ability', 'spell', 'harm_spell', 'help_spell'}
    wanted = set()
    for record in records:
        _, notes = localize_macro_blocks(record['blocks'], terms, record['check_data']['source_refs'])
        wanted.update(normalized(name) for note in notes for name in note['missing_names']
                      if normalized(name) not in placeholders and not name.startswith('@'))
    candidates = {}
    for record in records:
        for reference in record['check_data']['source_refs'].values():
            if normalized(reference.get('source_name', '')) in wanted:
                candidates[(reference['kind'], reference['id'])] = reference['source_name']
    for row in terms:
        if normalized(row['name_en']) in wanted and row['kind'] in {'spell', 'item'}:
            candidates.setdefault((row['kind'], row['object_id']), row['name_en'])

    def fetch(pair):
        kind, object_id = pair
        url = f'https://nether.wowhead.com/tooltip/{kind}/{object_id}?locale=4'
        response = requests.get(url, timeout=(10, 30))
        response.raise_for_status()
        payload = response.json()
        name = payload.get('name', '')
        if not re.search(r'[\u3400-\u9fff]', name):
            return None
        return {'game_version': '12.1.5', 'kind': kind, 'object_id': object_id,
                'name_en': candidates[pair], 'name_zh': name, 'icon': payload.get('icon', ''),
                'evidence': 'Wago 本地快照/既有词表未覆盖；按 Icy Veins 原文编号核验 Wowhead 中文名称：' + url}

    found, errors = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch, pair): pair for pair in candidates}
        for future in as_completed(futures):
            try:
                row = future.result()
                if row:
                    found.append(row)
            except Exception as exc:
                errors.append({'identity': futures[future], 'error': str(exc)})
    merged = {(row['kind'], row['object_id']): row for row in terms}
    merged.update({(row['kind'], row['object_id']): row for row in found})
    dump(folder / 'authoritative-terms.json', list(merged.values()))
    dump(folder / 'macro-name-lookups.json', {'terms': found, 'errors': errors,
                                            'unmapped_names': sorted(wanted - {normalized(row['name_en']) for row in found})})
    print(json.dumps({'待核验名称': len(wanted), '明确编号': len(candidates), '确认中文': len(found), '网络异常': len(errors)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
