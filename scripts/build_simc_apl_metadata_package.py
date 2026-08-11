#!/usr/bin/env python
"""把 SimC APL 字段清单生成可版本化、可幂等导入的数据包。"""

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / 'botend' / 'data' / 'simc_apl_metadata'
DEFAULT_LOCALIZATION_DIR = PROJECT_ROOT / 'botend' / 'data' / 'simc_apl_localization'


def parse_arguments():
    parser = argparse.ArgumentParser(
        description='从逐专精 SimC APL 字段清单生成中英文本地化数据库数据包。',
    )
    parser.add_argument('--input', required=True, help='阶段 42 生成的字段清单 JSON')
    parser.add_argument(
        '--output', default='',
        help='输出 JSON；默认按 revision/build 写入内置数据包目录',
    )
    parser.add_argument(
        '--settings',
        default=os.environ.get('DJANGO_SETTINGS_MODULE', 'LMonitor.settings'),
        help='加载模型所用的 Django settings 模块',
    )
    parser.add_argument(
        '--localization-overrides', default='',
        help='本地化覆盖 JSON；默认按 revision/build 从内置目录查找',
    )
    parser.add_argument(
        '--no-localization-overrides', action='store_true',
        help='不应用本地化覆盖，仅生成 Wowhead Spell ID 直连结果',
    )
    return parser.parse_args()


def _read_source(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'无法读取源字段清单 {path}: {exc}') from exc


def _default_output(payload):
    revision = str(payload.get('simc_revision') or '')[:12]
    build = str(payload.get('game_build') or '').replace('.', '_')
    return DEFAULT_OUTPUT_DIR / f'simc_apl_{revision}_{build}.json'


def _default_localization(payload):
    revision = str(payload.get('simc_revision') or '')[:12]
    build = str(payload.get('game_build') or '').replace('.', '_')
    return DEFAULT_LOCALIZATION_DIR / f'simc_apl_{revision}_{build}_zh.json'


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    temporary.replace(path)


def main():
    args = parse_arguments()
    sys.path.insert(0, str(PROJECT_ROOT))
    os.environ['DJANGO_SETTINGS_MODULE'] = args.settings

    import django

    django.setup()

    from botend.services.simc_apl.localization_enrichment import (
        apply_localization_overrides,
        load_localization_overrides,
    )
    from botend.services.simc_apl.metadata_package import (
        build_metadata_package,
        validate_metadata_package,
    )

    source_path = Path(args.input).expanduser().resolve()
    source_payload = _read_source(source_path)
    package = build_metadata_package(source_payload)
    localization_path = None
    if not args.no_localization_overrides:
        localization_path = (
            Path(args.localization_overrides).expanduser().resolve()
            if args.localization_overrides else _default_localization(package)
        )
        if localization_path.is_file():
            package = apply_localization_overrides(
                package, load_localization_overrides(localization_path),
            )
            validate_metadata_package(package)
        elif args.localization_overrides:
            raise RuntimeError(f'找不到本地化覆盖：{localization_path}')
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output else _default_output(package)
    )
    _atomic_write(output_path, package)
    counts = package['counts']
    print(
        f"数据包已生成：{output_path}\n"
        f"revision={package['simc_revision']} build={package['game_build']} "
        f"源记录={counts['source_record_count']} 入库事实={counts['fact_count']} "
        f"职业级={counts['scope_counts'].get('class', 0)} "
        f"专精级={counts['scope_counts'].get('spec', 0)} "
        f"有中文={counts['localized_count']} 缺中文={counts['missing_zh_count']}"
    )
    if localization_path and localization_path.is_file():
        print(f'已应用本地化覆盖：{localization_path}')


if __name__ == '__main__':
    main()
