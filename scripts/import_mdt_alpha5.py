#!/usr/bin/env python
"""在线上安全刷新 MythicDungeonTools 6.2.0-alpha5 数据。"""

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = (
    PROJECT_ROOT
    / 'botend'
    / 'data'
    / 'mythic_planner'
    / 'mdt_6_2_0_alpha5.json'
)
SOURCE_VERSION_KEY = 'mdt-6-2-0-alpha3'
TARGET_VERSION_KEY = 'mdt-6-2-0-alpha5'
SOURCE_TAG = '6.2.0-alpha5'
SOURCE_COMMIT = '94473b391b6fb7563f8466c4a596a11ef6218a12'
EXPECTED_COUNTS = {
    'dungeons': 16,
    'enemies': 467,
    'spawns': 3012,
    'abilities': 1648,
    'spells': 1459,
}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            '校验并导入 MDT 6.2.0-alpha5 内置数据包；首次升级会复用 '
            'alpha3 数据版本主键，保留路线、短链和人工编辑。'
        ),
    )
    parser.add_argument(
        '--settings',
        default=os.environ.get('DJANGO_SETTINGS_MODULE', 'LMonitor.settings'),
        help='Django settings 模块，默认读取 DJANGO_SETTINGS_MODULE。',
    )
    parser.add_argument(
        '--dry-run-only',
        action='store_true',
        help='只执行事务化校验并回滚，不正式写库。',
    )
    return parser.parse_args()


def validate_package():
    if not PACKAGE_PATH.is_file():
        raise RuntimeError(f'找不到内置数据包：{PACKAGE_PATH}')
    try:
        payload = json.loads(PACKAGE_PATH.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'内置数据包无法读取：{exc}') from exc
    version = payload.get('data_version') or {}
    metadata = version.get('metadata') or {}
    if version.get('key') != TARGET_VERSION_KEY:
        raise RuntimeError('数据包版本键不是预期的 alpha5。')
    if metadata.get('source_tag') != SOURCE_TAG:
        raise RuntimeError('数据包来源标签不是预期的 alpha5。')
    if metadata.get('source_commit') != SOURCE_COMMIT:
        raise RuntimeError('数据包来源提交与固定 alpha5 提交不一致。')

    dungeons = payload.get('dungeons') or []
    counts = {
        'dungeons': len(dungeons),
        'enemies': sum(len(row.get('enemies') or []) for row in dungeons),
        'spawns': sum(
            len(enemy.get('spawns') or [])
            for dungeon in dungeons
            for enemy in dungeon.get('enemies') or []
        ),
        'abilities': sum(
            len(enemy.get('abilities') or [])
            for dungeon in dungeons
            for enemy in dungeon.get('enemies') or []
        ),
        'spells': len({
            int(ability['spell_id'])
            for dungeon in dungeons
            for enemy in dungeon.get('enemies') or []
            for ability in enemy.get('abilities') or []
        }),
    }
    if counts != EXPECTED_COUNTS:
        raise RuntimeError(
            f'数据包计数异常：实际 {counts}，预期 {EXPECTED_COUNTS}。'
        )
    return counts


def database_counts(version):
    from botend.models import (
        MythicDungeonAbility,
        MythicDungeonEnemy,
        MythicDungeonSpawn,
    )

    return {
        'dungeons': version.dungeons.filter(is_active=True).count(),
        'enemies': MythicDungeonEnemy.objects.filter(
            dungeon__data_version=version,
            is_active=True,
        ).count(),
        'spawns': MythicDungeonSpawn.objects.filter(
            enemy__dungeon__data_version=version,
            is_active=True,
        ).count(),
        'abilities': MythicDungeonAbility.objects.filter(
            enemy__dungeon__data_version=version,
            is_active=True,
        ).count(),
        'spells': version.spells.filter(is_active=True).count(),
    }


def main():
    args = parse_arguments()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.environ['DJANGO_SETTINGS_MODULE'] = args.settings

    import django

    django.setup()

    from django.core.management import call_command

    from botend.models import MythicDungeonDataVersion

    package_counts = validate_package()
    source_exists = MythicDungeonDataVersion.objects.filter(
        key=SOURCE_VERSION_KEY,
    ).exists()
    target_exists = MythicDungeonDataVersion.objects.filter(
        key=TARGET_VERSION_KEY,
    ).exists()
    if source_exists and target_exists:
        raise RuntimeError(
            f'{SOURCE_VERSION_KEY} 与 {TARGET_VERSION_KEY} 同时存在，'
            '请先人工确认版本分叉，脚本不会自动合并。'
        )

    command_options = {
        'file_path': str(PACKAGE_PATH),
        'activate': True,
        'replace': True,
        'verbosity': 1,
    }
    if source_exists or target_exists:
        command_options['upgrade_from_version'] = SOURCE_VERSION_KEY

    print(f'数据包校验通过：{package_counts}')
    print('开始事务化 dry-run，所有数据库写入将在校验后回滚。')
    call_command(
        'import_mythic_dungeon_data',
        dry_run=True,
        **command_options,
    )
    if args.dry_run_only:
        print('dry-run 完成；未修改线上数据库。')
        return

    print('dry-run 通过，开始正式导入并激活 alpha5。')
    call_command(
        'import_mythic_dungeon_data',
        dry_run=False,
        **command_options,
    )
    version = MythicDungeonDataVersion.objects.get(
        key=TARGET_VERSION_KEY,
        is_active=True,
    )
    actual_counts = database_counts(version)
    if actual_counts != EXPECTED_COUNTS:
        raise RuntimeError(
            f'导入后数据库计数异常：实际 {actual_counts}，'
            f'预期 {EXPECTED_COUNTS}。'
        )
    print(f'alpha5 导入完成并通过数据库复核：{actual_counts}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'alpha5 导入失败：{exc}', file=sys.stderr)
        raise SystemExit(1) from exc
