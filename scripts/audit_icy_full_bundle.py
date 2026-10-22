"""检查完整初始化包的覆盖、校验值、正文结构、引用与代码保真。"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

from scripts.build_icy_full_bundle import ROOT, SPEC_IDENTITY_MAP, refs_preserved, load_terms
from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_markdown import compile_markdown
from botend.services.class_guide_icy import localize_macro_blocks
from django.core.management import call_command


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def inventory(markdown, terms=None, source_refs=None):
    compiled = compile_markdown(markdown)
    if terms:
        compiled, _ = localize_macro_blocks(compiled, terms, source_refs)
    blocks = list(walk_blocks(compiled))
    soup = BeautifulSoup('\n'.join(b.get('html', '') + b.get('title', '') for b in blocks), 'html.parser')
    return {
        'headings': sum(block['type'] == 'heading' for block in blocks),
        'tables': len(soup.find_all('table')),
        'rows': len(soup.find_all('tr')),
        'cells': len(soup.find_all(['td', 'th'])),
        'list_items': len(soup.find_all('li')),
        'preformatted': Counter(pre.get_text() for pre in soup.find_all('pre')),
        'codes': Counter((b['type'], b.get('data', {}).get('code', ''))
                         for b in blocks if b['type'] in {'code', 'talents'}),
        'assets': Counter((tag.name, key, tag[key]) for tag in soup.find_all(True)
                          for key in ('href', 'src') if tag.get(key)),
    }


def audit(path, destination):
    results, seen, total_chapters = [], set(), 0
    authoritative_terms = load_terms()
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        terms = archive.read('terms.json').decode('utf-8')
        if digest(terms) != manifest['terms_sha256']:
            raise ValueError('术语包摘要不一致')
        merged_terms = {(row['kind'], row['object_id']): row for row in authoritative_terms}
        merged_terms.update({(row['kind'], row['object_id']): row for row in json.loads(terms)})
        authoritative_terms = list(merged_terms.values())
        for entry in manifest['records']:
            stem = entry['slug'] + '-' + entry['game_version']
            draft = json.loads(archive.read(stem + '.json'))
            body = archive.read(stem + '.body.md').decode('utf-8')
            source = draft['source_markdown']
            spec_id = draft['spec_id']
            if spec_id in seen or SPEC_IDENTITY_MAP.get(spec_id) != (draft['class_name'], draft['spec_name']):
                raise ValueError('文章专精绑定异常')
            seen.add(spec_id)
            checks = {
                'markdown_hash': digest(draft['content_markdown']) == entry['markdown_sha256'],
                'body_hash': digest(body) == draft['source_payload']['body_sha256'],
                'body_embedded': draft['content_markdown'].endswith(body),
                'source_tag': 'icy' in draft['tags'],
                'credit': 'Icy Veins' in draft['content_markdown'] and draft['source_url'] in draft['content_markdown'],
                'refs_preserved': refs_preserved(source, body),
                'no_placeholders': not any(value in body for value in ('⟪K', '⟪S', '⟦WOWTERM_', '⟦S')),
            }
            before = inventory(source, authoritative_terms, draft['check_data']['source_refs'])
            after = inventory(body)
            for key in before:
                checks[key + '_preserved'] = before[key] == after[key]
            total_chapters += len(draft['source_payload']['chapters'])
            result = {'spec_id': spec_id, 'title': draft['title'], 'checks': checks,
                      'source_chars': len(source), 'translated_chars': len(body),
                      'source_parameter_warnings': draft['source_payload'].get('source_parameter_warnings', [])}
            results.append(result)
            print(json.dumps({'专精': spec_id, '通过': all(checks.values()),
                              '未通过': [key for key, value in checks.items() if not value]}, ensure_ascii=False), flush=True)
    if seen != set(SPEC_IDENTITY_MAP) or total_chapters != 400:
        raise ValueError('专精或章节覆盖不完整')
    report = {'bundle': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'spec_count': len(seen), 'chapter_count': total_chapters,
              'passed': all(all(row['checks'].values()) for row in results), 'records': results}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not report['passed']:
        raise ValueError('正文保真检查有未通过项，详见报告')
    call_command('import_class_guide_drafts', input_zip=str(path), dry_run=True)
    print('全部专精的格式、引用、代码与导入预校验通过。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    audit(args.input_zip, args.output)


if __name__ == '__main__':
    main()
