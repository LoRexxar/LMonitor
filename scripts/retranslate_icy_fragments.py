"""将可疑的短译文逐项独立重译，避免相邻片段在模型输出中发生合并。"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_icy_full_bundle import FileTranslator, dump, load_terms, fingerprint, translation_settings
from django.test import override_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--source-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provider', choices=('glm', 'mimo', 'codex'))
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--with-project-backup', action='store_true')
    parser.add_argument('--resume', action='store_true', help='复用本轮已经独立校验通过的片段')
    args = parser.parse_args()
    translator = FileTranslator(args.work_dir, load_terms(args.work_dir),
                                (args.provider,) if args.provider else None)
    selected = list(dict.fromkeys(row['source'] for row in json.loads(args.source_file.read_text(encoding='utf-8'))))
    completed, missing = {}, []
    for source in selected:
        path = args.work_dir / 'translation-cache' / (fingerprint('icy-full-v1\n' + source) + '.json')
        if path.exists():
            record = json.loads(path.read_text(encoding='utf-8'))
            if args.resume and not record.get('invalid_reason'):
                completed[source] = record['translated']
                continue
            record['invalid_reason'] = '原译文过短，需独立完整重译并逐条校验'
            dump(path, record)

    def one(source):
        for attempt in range(3):
            result = translator.batch([source], attempt=attempt)
            if source in result:
                return result[source]
        return None

    configs = translation_settings(args.with_project_backup)
    with override_settings(**configs), ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(one, source): source for source in selected if source not in completed}
        for number, future in enumerate(as_completed(futures), 1):
            source = futures[future]
            result = future.result()
            if result is None:
                missing.append(source)
            else:
                completed[source] = result
            if number % 20 == 0 or number == len(selected):
                print(json.dumps({'完成': number, '总数': len(selected), '通过': len(completed), '待核对': len(missing)}, ensure_ascii=False), flush=True)
    dump(args.output, completed)
    dump(args.output.with_suffix('.missing.json'), missing)
    if missing:
        raise RuntimeError(f'仍有 {len(missing)} 个片段需核对')


if __name__ == '__main__':
    main()
