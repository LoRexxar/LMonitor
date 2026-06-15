# -*- coding: utf-8 -*-

import json
import os


def load_db2_dump_map(dump_dir, table, id_field='ID'):
    """加载 dump_wago_db2_tables 生成的 jsonl 文件为 dict[ID] = row."""
    table = (table or '').strip()
    if not table:
        return {}
    file_path = os.path.join(dump_dir, f'{table}.jsonl')
    if not os.path.exists(file_path):
        return {}
    out = {}
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = (line or '').strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            try:
                rid = int(row.get(id_field) or row.get('id') or 0)
            except Exception:
                rid = 0
            if rid > 0:
                out[rid] = row
    return out


def load_db2_dump_rows(dump_dir, table):
    """加载 dump 为行列表（顺序读取，不建索引）。"""
    table = (table or '').strip()
    if not table:
        return []
    file_path = os.path.join(dump_dir, f'{table}.jsonl')
    if not os.path.exists(file_path):
        return []
    rows = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = (line or '').strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows
