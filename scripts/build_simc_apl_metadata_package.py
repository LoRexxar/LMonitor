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
DEFAULT_SOURCE_COVERAGE_DIR = PROJECT_ROOT / 'botend' / 'data' / 'simc_apl_source_coverage'

KIND_PREFIX = {
    'action': '',
    'buff': 'buff.',
    'debuff': 'debuff.',
    'dot': 'dot.',
    'cooldown': 'cooldown.',
}


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
    parser.add_argument(
        '--source-supplements', default='',
        help='源码静态覆盖补充 JSON；默认按 revision/build 从内置目录查找',
    )
    parser.add_argument(
        '--no-source-supplements', action='store_true',
        help='不合并源码静态覆盖补充，仅使用运行时 manifest 观测记录',
    )
    parser.add_argument(
        '--simc-source', default=str(PROJECT_ROOT / 'simc-source'),
        help='用于静态全集审计的锁定 SimC 源码目录',
    )
    parser.add_argument(
        '--no-source-coverage-audit', action='store_true',
        help='显式跳过 SimC 源码静态技能/Buff 全集差检查',
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


def _default_source_coverage(payload):
    revision = str(payload.get('simc_revision') or '')[:12]
    build = str(payload.get('game_build') or '').replace('.', '_')
    return DEFAULT_SOURCE_COVERAGE_DIR / f'simc_apl_{revision}_{build}_source.json'


def _source_labels(payload):
    class_labels = {}
    spec_labels = {}
    for record in payload.get('records') or []:
        class_name = str(record.get('class') or '').strip().lower()
        spec = str(record.get('spec') or '').strip().lower()
        if class_name and record.get('class_zh'):
            class_labels.setdefault(class_name, str(record['class_zh']).strip())
        if class_name and spec and record.get('spec_zh'):
            spec_labels.setdefault((class_name, spec), str(record['spec_zh']).strip())
    return class_labels, spec_labels


def _merge_source_supplements(source_payload, supplement_payload):
    """把源码静态支持项展开成与运行时 manifest 相同的逐专精记录。"""
    if not isinstance(supplement_payload, dict) or supplement_payload.get('schema_version') != 1:
        raise RuntimeError('源码静态覆盖补充 schema_version 必须为 1')
    if supplement_payload.get('package_type') != 'simc_apl_static_source_supplements':
        raise RuntimeError('源码静态覆盖补充 package_type 无效')
    for field in ('simc_revision', 'game_build'):
        if str(supplement_payload.get(field) or '') != str(source_payload.get(field) or ''):
            raise RuntimeError(f'源码静态覆盖补充的 {field} 与源字段清单不一致')
    definitions = supplement_payload.get('records')
    if not isinstance(definitions, list):
        raise RuntimeError('源码静态覆盖补充 records 必须为数组')
    official_specs = source_payload.get('official_specs') or {}
    expression_suffixes = source_payload.get('expression_suffixes') or {}
    class_labels, spec_labels = _source_labels(source_payload)
    existing = {
        (
            str(record.get('class') or '').strip().lower(),
            str(record.get('spec') or '').strip().lower(),
            str(record.get('kind') or '').strip().lower(),
            str(record.get('token') or '').strip().lower(),
        )
        for record in source_payload.get('records') or []
    }
    expanded = []
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            raise RuntimeError(f'源码静态覆盖 records[{index}] 必须为对象')
        class_name = str(definition.get('class') or '').strip().lower()
        kind = str(definition.get('kind') or '').strip().lower()
        token = str(definition.get('token') or '').strip().lower()
        supported_specs = official_specs.get(class_name)
        if not isinstance(supported_specs, list) or not supported_specs:
            raise RuntimeError(f'源码静态覆盖 records[{index}] 的职业无官方 APL 专精')
        requested_specs = definition.get('specs', supported_specs)
        if (not isinstance(requested_specs, list) or not requested_specs or
                any(spec not in supported_specs for spec in requested_specs)):
            raise RuntimeError(f'源码静态覆盖 records[{index}].specs 无效')
        if kind not in KIND_PREFIX or not token:
            raise RuntimeError(f'源码静态覆盖 records[{index}] 的 kind/token 无效')
        spell_id = definition.get('spell_id')
        if spell_id is not None and (
                not isinstance(spell_id, int) or isinstance(spell_id, bool) or spell_id <= 0):
            raise RuntimeError(f'源码静态覆盖 records[{index}].spell_id 无效')
        name_zh = str(definition.get('name_zh') or '').strip()
        if not name_zh:
            raise RuntimeError(f'源码静态覆盖 records[{index}].name_zh 不能为空')
        source_metadata = {
            key: value for key, value in definition.items()
            if key not in {'class', 'specs', 'kind', 'token', 'spell_id', 'name_zh'}
        }
        source_metadata.update({
            'coverage_source': 'simc_static_source',
            'source_revision': source_payload['simc_revision'],
        })
        suffixes = list(expression_suffixes.get(kind) or [])
        apl_field = f"{KIND_PREFIX[kind]}{token}"
        template = apl_field if kind == 'action' else (
            f"{apl_field}.{suffixes[0]}" if suffixes else apl_field
        )
        for spec in requested_specs:
            identity = (class_name, spec, kind, token)
            if identity in existing:
                raise RuntimeError(f'源码静态覆盖与运行时记录重复: {identity!r}')
            existing.add(identity)
            expanded.append({
                'class': class_name,
                'spec': spec,
                'kind': kind,
                'token': token,
                'spell_id': spell_id,
                'spell_id_candidates': [spell_id] if spell_id else [],
                'sources': ['static_source_registration'],
                'identity_reasons': ['源码静态注册补全'],
                'aliases': [],
                'action_options': [],
                'expression_suffixes': suffixes,
                'name_zh': name_zh,
                'localization_source': 'static_source_inference',
                'wowhead_status': 'inferred_official' if spell_id else 'inferred_semantic',
                'wowhead_raw_name': name_zh,
                'wowhead_url': f'https://www.wowhead.com/cn/spell={spell_id}' if spell_id else '',
                'apl_field': apl_field,
                'apl_expression_template': template,
                'class_zh': class_labels.get(class_name, class_name),
                'spec_zh': spec_labels.get((class_name, spec), spec),
                'identity_status': '已绑定' if spell_id else '未绑定',
                'source_metadata': source_metadata,
            })
    merged = dict(source_payload)
    merged['records'] = [*(source_payload.get('records') or []), *expanded]
    merged['source_supplements'] = {
        'policy': str(supplement_payload.get('policy') or ''),
        'definition_count': len(definitions),
        'expanded_record_count': len(expanded),
    }
    return merged, len(definitions), len(expanded)


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
    runtime_source_payload = source_payload
    source_coverage_path = None
    if not args.no_source_supplements:
        source_coverage_path = (
            Path(args.source_supplements).expanduser().resolve()
            if args.source_supplements else _default_source_coverage(source_payload)
        )
        if source_coverage_path.is_file():
            supplement_payload = _read_source(source_coverage_path)
            source_payload, definition_count, expanded_count = _merge_source_supplements(
                source_payload, supplement_payload,
            )
            print(
                f'已合并源码静态覆盖：{source_coverage_path} '
                f'定义={definition_count} 逐专精记录={expanded_count}'
            )
            if not args.no_source_coverage_audit:
                from audit_simc_apl_source_coverage import audit_source_coverage

                simc_source = Path(args.simc_source).expanduser().resolve()
                if not simc_source.is_dir():
                    raise RuntimeError(f'找不到用于全量覆盖审计的 SimC 源码：{simc_source}')
                audit = audit_source_coverage(
                    simc_source, runtime_source_payload, supplement_payload,
                )
                if audit['status'] != 'ok':
                    raise RuntimeError('SimC 源码字段覆盖审计失败：' + '；'.join(audit['failures']))
                print(
                    '源码字段覆盖审计通过：'
                    f"action={audit['counts']['covered_static_actions']}/"
                    f"{audit['counts']['static_action_candidates']} "
                    f"state={audit['counts']['covered_static_states']}/"
                    f"{audit['counts']['static_state_candidates']} "
                    f"dynamic={audit['counts']['covered_dynamic_state_calls']}/"
                    f"{audit['counts']['dynamic_state_calls']} "
                    f"action_literals="
                    f"{audit['counts']['classified_action_factory_non_token_literals']}/"
                    f"{audit['counts']['action_factory_non_token_literals']}"
                )
        elif args.source_supplements:
            raise RuntimeError(f'找不到源码静态覆盖补充：{source_coverage_path}')
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
