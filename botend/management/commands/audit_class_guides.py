"""对全部攻略草稿核对正文结构、引用、组件和翻译状态，并导出可迁移草稿。"""

import hashlib
import json
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.guide_models import ClassGuide
from botend.services.wow_localization import export_names
from botend.services.class_guide_content import REF_RE, walk_blocks
from botend.services.class_guide_service import check_article


def inventory(blocks):
    refs, assets, components = Counter(), Counter(), Counter()
    for block in walk_blocks(blocks):
        if block['type'] in {'gear', 'talents', 'rotation', 'priority', 'timeline', 'simulation', 'code'}:
            components[block['type']] += 1
        soup = BeautifulSoup(block.get('html', '') + block.get('title', ''), 'html.parser')
        for code in soup.select('code, pre'):
            code.decompose()
        refs.update(match[0] for match in REF_RE.finditer(soup.get_text()))
        assets.update('图片:' + tag['src'] for tag in soup.select('img[src]'))
        assets.update('链接:' + tag['href'] for tag in soup.select('a[href]'))
    return refs, assets, components


class Command(BaseCommand):
    help = '逐篇核对首次中文草稿的引用、链接、组件和待翻译状态；导出 Markdown 及原文快照'

    def add_arguments(self, parser):
        parser.add_argument('--output-dir', required=True)
        parser.add_argument('--game-version', default='')

    def handle(self, *args, **options):
        folder = Path(options['output_dir']); folder.mkdir(parents=True, exist_ok=True)
        guides = ClassGuide.objects.order_by('slug', 'game_version')
        if options['game_version']:
            guides = guides.filter(game_version=options['game_version'])
        records = []
        for guide in guides:
            audit = check_article(guide)
            source = inventory(guide.source_blocks)
            translated = inventory(guide.blocks)
            differences = {name: {'missing': dict(before - after), 'extra': dict(after - before)}
                for name, before, after in zip(['references', 'assets', 'components'], source, translated) if before != after}
            status = 'complete' if audit['complete'] and not differences else 'issues'
            record = {'slug': guide.slug, 'game_version': guide.game_version, 'guide_id': guide.id,
                'spec_id': guide.spec_id, 'is_visible': not guide.archived,
                'tags': list(guide.tags.values_list('name', flat=True)),
                'status': status, 'untranslated_count': len(audit['untranslated']),
                'unresolved_references': audit['unresolved_references'], 'unsupported_components': audit['unsupported_blocks'],
                'source_name_mismatches': audit['source_name_mismatches'], 'historical_references': audit['historical_references'],
                'source_macro_repairs': audit['source_macro_repairs'],
                'structural_differences': differences, 'markdown_sha256': hashlib.sha256(guide.content_markdown.encode()).hexdigest()}
            records.append(record)
            record['tags_sha256'] = hashlib.sha256(json.dumps(record['tags'], ensure_ascii=False).encode()).hexdigest()
            record['source_author_profile'] = guide.source_author_profile
            record['author_profile'] = guide.author_profile
            record['author_profiles_sha256'] = hashlib.sha256(json.dumps(
                [guide.source_author_profile, guide.author_profile], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            stem = guide.slug + '-' + guide.game_version
            (folder / (stem + '.md')).write_text(guide.content_markdown, encoding='utf-8')
            (folder / (stem + '.source.md')).write_text(guide.source_markdown, encoding='utf-8')
            (folder / (stem + '.json')).write_text(json.dumps({**record, 'title': guide.title,
                'class_name': guide.class_name, 'spec_name': guide.spec_name, 'guide_type': guide.guide_type,
                'author': guide.author, 'source_url': guide.source_url, 'source_hash': guide.source_hash,
                'source_modified': guide.source_modified, 'content_markdown': guide.content_markdown,
                'source_markdown': guide.source_markdown, 'source_payload': guide.source_payload,
                'check_data': audit}, ensure_ascii=False), encoding='utf-8')
        summary = {'created_at': timezone.now().isoformat(), 'total': len(records),
            'status_counts': dict(Counter(row['status'] for row in records)),
            'translation_complete': sum(not row['untranslated_count'] for row in records),
            'structural_failures': sum(bool(row['structural_differences']) for row in records), 'records': records}
        terms = export_names({row['game_version'] for row in records})
        term_text = json.dumps(terms, ensure_ascii=False)
        (folder / 'terms.json').write_text(term_text, encoding='utf-8')
        summary['terms_sha256'] = hashlib.sha256(term_text.encode()).hexdigest()
        (folder / 'manifest.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(json.dumps({k: v for k, v in summary.items() if k != 'records'}, ensure_ascii=False))
