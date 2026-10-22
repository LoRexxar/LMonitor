"""把本地提供的章节文件合并为待翻译的攻略正文及结构检查数据。"""

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand, CommandError

from botend.constants.wow import CLASS_CN, SPEC_CN, resolve_spec_identity
from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_icy import merge_chapters


class Command(BaseCommand):
    help = '将本地提供的 Icy 章节 HTML 转为本站 Markdown 和待翻译块，不写入数据库'

    def add_arguments(self, parser):
        parser.add_argument('--input-manifest', required=True, help='单专精章节文件清单')
        parser.add_argument('--output-dir', required=True, help='新的输出目录，不能覆盖已有目录')

    def handle(self, *args, **options):
        source = Path(options['input_manifest']).resolve()
        destination = Path(options['output_dir']).resolve()
        if destination.exists():
            raise CommandError('输出目录已存在，请使用新目录')
        try:
            manifest = json.loads(source.read_text(encoding='utf-8'))
            spec_id, cls, spec = resolve_spec_identity(manifest['spec_id'])
            if not isinstance(manifest.get('game_version'), str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', manifest['game_version']):
                raise ValueError('必须指定来源版本')
            slug_parts = [re.sub(r'(?<=[a-z])(?=[A-Z])', '-', value).lower() for value in (spec, cls)]
            expected_prefix = '/wow/' + '-'.join(slug_parts) + '-'
            chapters = []
            for row in manifest['chapters']:
                if not urlparse(row['source_url']).path.startswith(expected_prefix):
                    raise ValueError('章节来源与所选职业专精不一致')
                filename = (source.parent / row['file']).resolve()
                if not filename.is_relative_to(source.parent) or filename.suffix.lower() not in {'.html', '.htm'}:
                    raise ValueError('章节必须使用清单目录下的 HTML 文件')
                if filename.stat().st_size > 8000000:
                    raise ValueError('单章节文件超过 8 MB')
                chapters.append({'source_url': row['source_url'], 'title': row['title'],
                                 'html': filename.read_text(encoding='utf-8')})
            converted = merge_chapters(chapters)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise CommandError('正文转换失败：' + str(exc)) from exc
        fields = []
        for block in walk_blocks(converted['blocks']):
            # 结构、JSON、天赋代码、宏命令不进入翻译字段。
            for key in ('title', 'html'):
                if block.get(key):
                    fragment = BeautifulSoup(block[key], 'html.parser')
                    for index, text in enumerate(fragment.find_all(string=True)):
                        if str(text).strip() and not text.find_parent(['pre', 'code']):
                            fields.append({'id': block['id'], 'field': key, 'node_index': index,
                                           'source': str(text)})
        metadata = {key: manifest[key] for key in ('game_version', 'author') if key in manifest}
        metadata.update(spec_id=spec_id, class_name=cls, spec_name=spec,
                        title=f'{CLASS_CN[cls]}·{SPEC_CN[spec]}攻略', tags=['icy', manifest['game_version']],
                        conversion=converted, translation_fields=fields,
                        status='结构待检查' if converted['check_data']['unsupported'] else '正文格式已转换，待翻译')
        destination.mkdir(parents=True)
        (destination / 'source.md').write_text(converted['content_markdown'], encoding='utf-8')
        (destination / 'conversion.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.stdout.write(f'已转换 {len(chapters)} 个章节，{len(fields)} 个待翻译字段，'
                          f'{len(converted["check_data"]["unsupported"])} 项结构问题：{destination}')
