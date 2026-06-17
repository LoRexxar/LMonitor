#!/usr/bin/env python3
"""批量从 wago.tools 查询 FileDataID → icon name 映射"""
import csv
import os
import re
import sys
import time

import requests

DUMP_DIR = '.cache/wago_db2_dumps/latest'
CACHE_FILE = os.path.join(DUMP_DIR, 'file_data_icon_cache.csv')
IDS_FILE = os.path.join(DUMP_DIR, 'needed_file_data_ids.txt')

def main():
    file_data_ids = []
    with open(IDS_FILE) as f:
        for line in f:
            file_data_ids.append(int(line.strip()))

    icon_cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            for row in csv.DictReader(f):
                icon_cache[int(row['FileDataID'])] = row['IconName']
        print(f'Loaded {len(icon_cache)} cached icons', flush=True)

    to_query = [fid for fid in file_data_ids if fid not in icon_cache]
    print(f'Total: {len(file_data_ids)}, cached: {len(icon_cache)}, to query: {len(to_query)}', flush=True)

    queried = 0
    found = 0
    pattern = re.compile(r'filename&quot;:&quot;([^&]+?)\.blp&quot;', re.I)

    for file_data_id in to_query:
        url = f'https://wago.tools/files?search={file_data_id}'
        try:
            r = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
            matches = pattern.findall(r.text)
            icon_name = ''
            for raw in matches:
                path = raw.replace('\\/', '/').lower()
                if '/icons/' not in path:
                    continue
                base = os.path.basename(path)
                icon_name = base
                break
            icon_cache[file_data_id] = icon_name
            if icon_name:
                found += 1
        except Exception:
            icon_cache[file_data_id] = ''

        queried += 1
        if queried % 50 == 0:
            # 中间保存
            with open(CACHE_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['FileDataID', 'IconName'])
                for fid, name in sorted(icon_cache.items()):
                    writer.writerow([fid, name])
            print(f'Progress: {queried}/{len(to_query)}, found {found}', flush=True)
        time.sleep(0.15)

    # 最终保存
    with open(CACHE_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['FileDataID', 'IconName'])
        for fid, name in sorted(icon_cache.items()):
            writer.writerow([fid, name])

    total_found = sum(1 for v in icon_cache.values() if v)
    print(f'DONE: {queried} queried, {found} new found, total {total_found}/{len(icon_cache)} with icons', flush=True)

if __name__ == '__main__':
    main()
