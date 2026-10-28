"""导出未审计原始 DBC 与正常初始化原生关系；不依赖或放宽正式 scope 契约。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess


SOURCE_FILES = (
    'engine/sc_main.cpp', 'engine/player/player.cpp', 'engine/action/parse_effects.cpp',
    'engine/dbc/spell_data.cpp', 'engine/sim/sim.cpp', 'engine/sim/sim.hpp',
    'engine/dbc/generated/client_data_version.inc',
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run_step(command, output, identity, timeout=180):
    """Always persist complete byte logs and a receipt, including launch/timeout failures."""
    row = {'命令': command, 'stdout': identity + '.stdout.log', 'stderr': identity + '.stderr.log'}
    stdout, stderr = b'', b''
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
        stdout, stderr = result.stdout, result.stderr
        row['退出码'] = result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout, stderr = getattr(exc, 'stdout', None) or b'', getattr(exc, 'stderr', None) or b''
        row.update({'退出码': 124 if isinstance(exc, subprocess.TimeoutExpired) else 127,
                    '执行异常': type(exc).__name__ + ': ' + str(exc)})
    (output / row['stdout']).write_bytes(stdout)
    (output / row['stderr']).write_bytes(stderr)
    if row['退出码']:
        # Full error text is retained: review consumers compare known unsupported errors exactly.
        row['失败原因'] = (stderr or stdout).decode('utf-8', errors='replace') or row.get('执行异常', '')
    write_json(output / (identity + '.receipt.json'), row)
    return row


def validate_raw_catalog(data, source, revision, build, resolve_revision=None):
    """Resolve compiled git identity; CLI/source labels alone never attest a binary.

    The raw file is kept verbatim. The legacy consumer view uses null (unknown),
    not [] (classified empty), for its required old-rule display-only field.
    """
    if (not isinstance(data, dict) or type(data.get('raw_catalog_schema')) is not int
            or data['raw_catalog_schema'] != 1 or data.get('catalog_kind') != 'raw_dbc_scope_evidence'
            or data.get('draft') is not True or data.get('review_status') != 'unreviewed'
            or data.get('compile_contract_allowed') is not False or data.get('normalized') is not False
            or data.get('ptr') is not False):
        raise ValueError('必须使用独立、未经归一化且未复核的 live DBC 原始目录')
    forbidden = {'global_effect_indices', 'global_components', 'remove_talent', 'talents',
                 'scope_contract_sha256', 'reviewed_global_effects', 'normalization_basis'}
    def reject_classification(value):
        if isinstance(value, dict):
            if forbidden.intersection(value):
                raise ValueError('原始 DBC 目录不能携带旧 scope 分类或正式契约字段')
            for child in value.values():
                reject_classification(child)
        elif isinstance(value, list):
            for child in value:
                reject_classification(child)
    reject_classification(data)
    binary_revision = data.get('binary_revision')
    if (not isinstance(binary_revision, str) or not re.fullmatch(r'[0-9a-f]{7,40}', binary_revision)
            or data.get('simc_revision') != binary_revision
            or not re.fullmatch(r'[0-9a-f]{40}', revision)
            or not revision.startswith(binary_revision) or data.get('game_build') != build):
        raise ValueError('编译二进制 revision/build 与审计源码不一致')
    if resolve_revision is None:
        resolve_revision = lambda value: subprocess.check_output(
            ['git', '-C', str(source), 'rev-parse', '--verify', value + '^{commit}'],
            text=True, stderr=subprocess.PIPE).strip()
    if resolve_revision(binary_revision) != revision:
        raise ValueError('编译 revision 不能在源码仓库唯一解析为当前提交')
    rows = data.get('talent_catalog')
    if not isinstance(rows, list) or not rows:
        raise ValueError('原始 DBC 目录缺少天赋记录')
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('effects'), list)
                or not isinstance(row.get('dbc_scope_effects'), list)):
            raise ValueError('原始 DBC 天赋效果结构无效')
    return {**data, 'simc_revision': revision, 'source_revision': revision,
            'revision_resolution': 'compiled_git_revision_resolved_in_source_repository',
            'legacy_classification': 'unavailable; null is not an empty reviewed set',
            'talent_catalog': [{**row, 'global_effect_indices': None} for row in rows]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--catalog-only', action='store_true', help='只采集目录，不重跑或声称覆盖 actor 审计')
    args = parser.parse_args()
    if args.workers < 1 or (not args.inputs and not args.catalog_only) or (args.inputs and args.catalog_only):
        parser.error('需要 --inputs 或独立 --catalog-only，workers 必须为正整数')
    args.output.mkdir(parents=True, exist_ok=False)
    binary = args.binary.resolve()
    manifest = {'说明': '未复核原始证据；不是正式 scope 契约，不清除全局效果。未观察不等于无影响。',
                'draft': True, 'review_status': 'unreviewed', 'compile_contract_allowed': False,
                'normalized': False, '模式': 'catalog_only' if args.catalog_only else 'native_audit',
                '状态': 'failed', '结果': []}

    def git_step(name, *arguments):
        row = run_step(['git', '-C', str(args.source), *arguments], args.output, name)
        if row['退出码']:
            raise ValueError('源码身份检查失败：' + name)
        return (args.output / row['stdout']).read_bytes()

    try:
        revision = git_step('source-revision', 'rev-parse', 'HEAD').decode().strip()
        version = (args.source / 'engine/dbc/generated/client_data_version.inc').read_text()
        match = re.search(r'#define CLIENT_DATA_WOW_VERSION "([^"]+)"', version)
        if not re.fullmatch(r'[0-9a-f]{40}', revision) or not match:
            raise ValueError('源码 revision/build 无效')
        build = match[1]
        manifest.update({'源码提交': revision, '客户端版本': build, '二进制摘要': sha256(binary),
                         '源码文件摘要': {name: sha256(args.source / name) for name in SOURCE_FILES},
                         '本地修改摘要': hashlib.sha256(git_step('source-diff', 'diff', '--binary', 'HEAD')).hexdigest()})
        raw = args.output / 'raw-dbc-scope-catalog.json'
        catalog = args.output / 'global-scope-catalog.json'
        step = run_step([str(binary), f'skill_damage_raw_scope_export={raw.resolve()}'], args.output, 'catalog')
        manifest['目录导出'] = step
        if step['退出码']:
            raise ValueError('原始目录导出失败，见 catalog.stderr.log 与 catalog.receipt.json')
        view = validate_raw_catalog(json.loads(raw.read_text(encoding='utf-8')), args.source, revision, build,
            resolve_revision=lambda value: git_step('binary-revision', 'rev-parse', '--verify', value + '^{commit}').decode().strip())
        write_json(catalog, view)
        manifest.update({'原始DBC目录摘要': sha256(raw), 'DBC目录摘要': sha256(catalog),
                         '编译revision': view['binary_revision']})
        inputs = [(directory, path) for directory in args.inputs for path in sorted(directory.glob('*.simc'))]
        identities = [directory.name + '--' + path.stem for directory, path in inputs]
        if len(set(identities)) != len(identities) or (not inputs and not args.catalog_only):
            raise ValueError('原生审计输入为空或结果名称重复')

        def run(item):
            directory, path = item
            identity = directory.name + '--' + path.stem
            output = args.output / (identity + '.json')
            row = {'输入': str(path), '结果文件': output.name}
            try:
                row['输入摘要'] = sha256(path)
                row.update(run_step([str(binary), str(path.resolve()),
                    f'skill_damage_evidence_export={output.resolve()}', 'report_details=1'], args.output, identity))
                if row['退出码'] == 0:
                    data = json.loads(output.read_text(encoding='utf-8'))
                    if data.get('normalized') is not False or data.get('evidence_schema') != 1:
                        raise ValueError('不是未经归一化的原生证据')
                    row['原生关联数'] = sum(len(a['bindings']) for a in data['actors'])
                    row['已选天赋数'] = sum(len(a['selected_trait_ids']) for a in data['actors'])
                    row['结果摘要'] = sha256(output)
                if sha256(path) != row['输入摘要']:
                    raise ValueError('原生输入在执行期间发生变化')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row.update({'进程退出码': row.get('退出码'), '退出码': 1,
                            '校验失败': type(exc).__name__ + ': ' + str(exc), '失败原因': str(exc)})
            write_json(args.output / (identity + '.receipt.json'), row)
            print(identity, '成功' if row['退出码'] == 0 else '导出失败', flush=True)
            return row

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            manifest['结果'] = list(pool.map(run, inputs))
        if (sha256(binary) != manifest['二进制摘要']
                or git_step('source-revision-after', 'rev-parse', 'HEAD').decode().strip() != revision
                or hashlib.sha256(git_step('source-diff-after', 'diff', '--binary', 'HEAD')).hexdigest() != manifest['本地修改摘要']
                or any(sha256(args.source / name) != digest for name, digest in manifest['源码文件摘要'].items())):
            raise ValueError('采集期间二进制或源码身份发生变化')
        manifest['状态'] = ('failed' if any(row['退出码'] for row in manifest['结果'])
                            else 'catalog_only' if args.catalog_only else 'complete')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        manifest['失败原因'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        write_json(args.output / 'manifest.json', manifest)
    print('状态', manifest['状态'], '成功', sum(r['退出码'] == 0 for r in manifest['结果']),
          '失败', sum(r['退出码'] != 0 for r in manifest['结果']))
    return int(manifest['状态'] == 'failed')


if __name__ == '__main__':
    raise SystemExit(main())
