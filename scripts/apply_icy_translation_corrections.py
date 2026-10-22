"""保存逐条核对的完整译文，并使依赖这些片段的正文缓存重新汇总。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_icy_full_bundle import FileTranslator, dump, field_values, fingerprint, load_terms, refs_preserved, structure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--corrections', type=Path, required=True)
    args = parser.parse_args()
    folder = args.work_dir
    translator = FileTranslator(folder, load_terms(folder))
    corrections = json.loads(args.corrections.read_text(encoding='utf-8'))
    for source, translated in corrections.items():
        if not refs_preserved(source, translated) or structure(source) != structure(translated):
            raise ValueError('校订内容改变了引用或结构：' + source)
        translator.save(source, translated)
    invalidated = 0
    for path in (folder / 'converted').glob('*.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        for value in set(field_values(record)):
            if value in corrections or not any(unit in corrections for unit in translator.segments(value)):
                continue
            cache = folder / 'translation-cache' / (fingerprint('icy-full-v1\n' + value) + '.json')
            if cache.exists():
                row = json.loads(cache.read_text(encoding='utf-8'))
                row['invalid_reason'] = '下属文本片段已经逐条校订，需重新汇总完整正文'
                dump(cache, row)
                invalidated += 1
    print(json.dumps({'校订片段': len(corrections), '重新汇总字段': invalidated}, ensure_ascii=False))


if __name__ == '__main__':
    main()
