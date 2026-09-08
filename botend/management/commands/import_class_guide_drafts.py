"""将已完成翻译的预写入包导入目标环境，无需重新调用翻译服务。"""

import copy
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile, BadZipFile

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.core.exceptions import ValidationError

from botend.guide_models import ClassGuide
from botend.services.wow_localization import validate_name, write_name
from botend.services.class_guide_markdown import compile_markdown, normalize_tab_markdown
from botend.services.class_guide_service import save_article, has_manual_content
from botend.services.class_guide_tags import ensure_source_tag, normalize_tags, set_guide_tags, source_labels
from botend.services.class_guide_authors import normalize_author_profile


class Command(BaseCommand):
    help = '导入 audit_class_guides 导出的 Markdown 草稿及术语包，保留目标环境人工正文'

    def add_arguments(self, parser):
        inputs = parser.add_mutually_exclusive_group()
        inputs.add_argument('--input-dir', help='已解压的草稿包目录')
        inputs.add_argument('--input-zip', help='草稿 ZIP 包；不指定时使用仓库内置中文包')
        parser.add_argument('--dry-run', action='store_true', help='校验完整包，不写入数据库')

    def handle(self, *args, **options):
        try:
            if options['input_dir']:
                folder = Path(options['input_dir']).resolve()
                return self.import_bundle(lambda name: (folder / name).read_text(encoding='utf-8'), options['dry_run'])
            archive = options['input_zip'] or Path(__file__).resolve().parents[2] / 'data/class_guides/initial-drafts-20260908.zip'
            with ZipFile(archive) as bundle:
                return self.import_bundle(lambda name: bundle.read(name).decode('utf-8'), options['dry_run'])
        except (OSError, BadZipFile, KeyError, ValueError) as exc:
            raise CommandError(f'读取草稿包失败：{exc}') from exc

    def import_bundle(self, read_text, dry_run):
        manifest = json.loads(read_text('manifest.json'))
        term_text = read_text('terms.json')
        if hashlib.sha256(term_text.encode()).hexdigest() != manifest.get('terms_sha256'):
            raise CommandError('术语文件校验失败')
        terms, drafts, identities = json.loads(term_text), [], set()
        for entry in manifest['records']:
            slug, version = entry['slug'], entry['game_version']
            if not re.fullmatch(r'[a-z0-9_-]+', slug) or not re.fullmatch(r'[A-Za-z0-9_.-]+', version):
                raise CommandError('文章标识或版本包含非法字符')
            if (slug, version) in identities:
                raise CommandError('包内存在重复文章')
            identities.add((slug, version))
            draft = json.loads(read_text(slug + '-' + version + '.json'))
            if draft['slug'] != slug or draft['game_version'] != version:
                raise CommandError('文章身份与清单不一致')
            digest = hashlib.sha256(draft['content_markdown'].encode()).hexdigest()
            if digest != entry['markdown_sha256']:
                raise CommandError('正文校验失败：' + slug)
            tags = normalize_tags(draft.get('tags', source_labels(version, draft['guide_type'])))
            if entry.get('tags_sha256') and hashlib.sha256(json.dumps(tags, ensure_ascii=False).encode()).hexdigest() != entry['tags_sha256']:
                raise CommandError('标签校验失败：' + slug)
            draft['tags'] = tags
            visible = draft.get('is_visible', True)
            if not isinstance(visible, bool) or ('is_visible' in entry and entry['is_visible'] != visible):
                raise CommandError('文章显示设置无效或与清单不一致：' + slug)
            draft['is_visible'] = visible
            source_author = draft.get('source_author_profile', {})
            custom_author = draft.get('author_profile')
            if entry.get('author_profiles_sha256') and hashlib.sha256(json.dumps(
                    [source_author, custom_author], ensure_ascii=False, sort_keys=True).encode()).hexdigest() != entry['author_profiles_sha256']:
                raise CommandError('作者资料校验失败：' + slug)
            draft['source_author_profile'] = normalize_author_profile(source_author or {'name': draft['author']})
            draft['author_profile'] = normalize_author_profile(custom_author) if custom_author is not None else None
            compile_markdown(draft['content_markdown']); compile_markdown(draft['source_markdown'])
            draft['content_markdown'] = normalize_tab_markdown(draft['content_markdown'])
            draft['source_markdown'] = normalize_tab_markdown(draft['source_markdown'])
            guide = ClassGuide(**{key: draft[key] for key in ['slug', 'game_version', 'title', 'class_name', 'spec_name', 'guide_type', 'author', 'source_url']})
            guide.spec_id = draft.get('spec_id')
            try:
                guide.full_clean(validate_unique=False, validate_constraints=False)
            except ValidationError as exc:
                raise CommandError(f'文章专精或字段校验失败：{slug}：{exc}') from exc
            if entry.get('spec_id', guide.spec_id) != guide.spec_id:
                raise CommandError('专精编号与清单不一致：' + slug)
            draft.update(spec_id=guide.spec_id, class_name=guide.class_name, spec_name=guide.spec_name)
            drafts.append(draft)
        for row in terms:
            if row['kind'] not in ('spell', 'talent', 'item', 'phrase', 'macro') or not re.search(r'[\u3400-\u9fff]', row['name_zh']):
                raise CommandError('术语内容无效')
            validate_name(row)
        if dry_run:
            self.stdout.write('校验通过：{} 篇草稿，{} 条术语'.format(len(drafts), len(terms)))
            return
        term_conflicts, imported, skipped = 0, 0, 0
        for row in terms:
            record, _ = write_name(row)
            term_conflicts += record['name_zh'] != row['name_zh']
        for draft in drafts:
            with transaction.atomic():
                guide, created = ClassGuide.objects.get_or_create(slug=draft['slug'], game_version=draft['game_version'],
                    defaults={'archived': not draft['is_visible'], **{k: draft[k] for k in ('title', 'spec_id', 'class_name', 'spec_name', 'guide_type', 'author', 'source_url', 'source_author_profile', 'author_profile')}})
                if guide.spec_id != draft['spec_id']:
                    skipped += 1
                    continue
                if created:
                    set_guide_tags(guide, draft['tags'])
                    ensure_source_tag(guide)
                elif 'source_author_profile' in draft and draft['source_author_profile'].get('avatar'):
                    ClassGuide.objects.filter(pk=guide.pk).update(source_author_profile=draft['source_author_profile'])
                existing = (guide.content_markdown == draft['content_markdown'] and guide.title == draft['title'] and guide.source_hash == draft['source_hash'])
                if existing or has_manual_content(guide):
                    skipped += 1
                    continue
                save_article(guide.id, draft['title'], content_markdown=draft['content_markdown'], expected_updated_at=guide.updated_at,
                    imported=True, source_markdown=draft['source_markdown'],
                    source_payload=draft['source_payload'], source_hash=draft['source_hash'], source_modified=draft['source_modified'],
                    check_data=draft.get('check_data', draft.get('audit', {})))
                imported += 1
        self.stdout.write('导入 {} 篇，跳过 {} 篇；保留目标环境 {} 项术语冲突'.format(imported, skipped, term_conflicts))
