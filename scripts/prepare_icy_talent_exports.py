"""缓存来源计算器的数据，复用其公开导出逻辑生成游戏天赋导入串。"""

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    folder = args.work_dir.resolve()
    target = folder / 'talent-data'
    target.mkdir(exist_ok=True)

    def fetch(name):
        path = target / (name + '.json')
        if not path.exists():
            response = requests.get('https://static.icy-veins.com/json/midnight-talent-calculator/' + name + '.json?v=44', timeout=(10, 30))
            response.raise_for_status()
            path.write_text(json.dumps(response.json(), ensure_ascii=False), encoding='utf-8')
        return json.loads(path.read_text(encoding='utf-8'))

    classes = fetch('classes_basic_info')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, ['pvp-talents', *(row['name'] for row in classes)]))
    subprocess.run(['node', str(Path(__file__).with_name('export_icy_talents.cjs')), str(folder)], check=True)


if __name__ == '__main__':
    main()
