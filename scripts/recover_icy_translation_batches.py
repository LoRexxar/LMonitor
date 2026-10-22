"""从已保存的编号响应中恢复完整条目，不发送新翻译请求。"""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_icy_full_bundle import FileTranslator, dump, load_terms


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    translator = FileTranslator(args.work_dir, load_terms())
    save = translator.save
    recovered = 0

    def save_missing(source, translated):
        nonlocal recovered
        if translator.cached(source) is None:
            save(source, translated)
            recovered += 1

    translator.save = save_missing
    reviewed = 0
    for path in (args.work_dir / 'translation-batches').glob('*.json'):
        batch = json.loads(path.read_text(encoding='utf-8'))
        if not batch.get('answer') or not any(translator.cached(value) is None for value in batch['sources']):
            continue
        translator.local.service = SimpleNamespace(
            engine=SimpleNamespace(send_message=lambda *a, answer=batch['answer'], **kw: answer), last_error='')
        translator.batch(batch['sources'], attempt=2, record_response=False)
        reviewed += 1
    report = {'reviewed_batches': reviewed, 'recovered_fields': recovered,
              '说明': '仅重新解析已有响应，完整保留编号并重新检查术语和引用；没有发送新请求。'}
    dump(args.work_dir / 'batch-recovery-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
