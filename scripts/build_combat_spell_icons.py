"""从同一正式服构建的两份 DB2 表生成日志分析随版本发布的只读图标索引。"""

import argparse
import csv
import io
import re
import sqlite3
from pathlib import Path

import requests


def table(build, name):
    with requests.get(f"https://wago.tools/db2/{name}/csv", params={"build": build},
                      stream=True, timeout=(10, 120)) as response:
        response.raise_for_status()
        response.raw.decode_content = True
        yield from csv.DictReader(io.TextIOWrapper(response.raw, encoding="utf-8-sig", newline=""))


def build_index(build):
    icons = {}
    for row in table(build, "ManifestInterfaceData"):
        if row["FilePath"].strip("\\").lower() != "interface\\icons":
            continue
        match = re.fullmatch(r"([a-z0-9_-]{1,160})\.blp", row["FileName"].lower())
        if match and match[1] != "inv_misc_questionmark":
            icons[int(row["ID"])] = match[1]
    spells = {}
    for row in table(build, "SpellMisc"):
        if row["DifficultyID"] == "0":
            fid = int(row["SpellIconFileDataID"] or 0) or int(row["ActiveIconFileDataID"] or 0)
            if fid in icons:
                spells[int(row["SpellID"])] = fid
    if not spells:
        raise ValueError("两份游戏数据表没有匹配的技能图标。")
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE version(build TEXT NOT NULL);
            CREATE TABLE icon(file_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE spell(id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL);
        """)
        db.execute("INSERT INTO version VALUES (?)", (build,))
        db.executemany("INSERT INTO icon VALUES (?,?)", sorted(icons.items()))
        db.executemany("INSERT INTO spell VALUES (?,?)", sorted(spells.items()))
        return db.serialize(), len(spells)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", help="正式服构建号，例如 12.1.0.69404")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", args.build):
        parser.error("请输入完整的游戏构建号。")
    data, count = build_index(args.build)
    output = Path(__file__).resolve().parents[1] / "botend/data/combat_spell_icons.sqlite3"
    output.write_bytes(data)
    print(f"已生成 {count} 条技能图标映射，构建 {args.build}。")
