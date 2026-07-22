import json
import re

from django.core.management.base import BaseCommand
from django.db import transaction

from botend.models import MonitorTask, WowArticle
from botend.services.article_content_service import blocks_to_plain_text, dumps_blocks, extract_structured_article, loads_blocks
from botend.services.article_translation_service import build_translation_service


TEXT_TYPES = {'paragraph', 'heading', 'quote', 'list_item'}


class Command(BaseCommand):
    help = "Repair Wowhead articles whose inline links/icons were split into hard line breaks."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write changes to database. Default is dry-run.')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--id', type=int, action='append', dest='ids')
        parser.add_argument('--reset-translation', action='store_true', help='Clear Chinese content fields after source content changes.')
        parser.add_argument('--force', action='store_true', help='Rewrite even if content does not look suspicious.')
        parser.add_argument('--refetch-only', action='store_true', help='Do not repair from existing content when remote fetch fails.')
        parser.add_argument('--translate', action='store_true', help='Translate repaired articles after saving source content.')

    def handle(self, *args, **options):
        apply_changes = bool(options.get('apply'))
        limit = int(options.get('limit') or 0)
        ids = options.get('ids') or []
        reset_translation = bool(options.get('reset_translation'))
        force = bool(options.get('force'))
        refetch_only = bool(options.get('refetch_only'))
        translate_after = bool(options.get('translate'))
        translation_service = build_translation_service() if translate_after else None
        fetcher = self._build_wowhead_fetcher()

        qs = WowArticle.objects.filter(source='wowhead').exclude(url__isnull=True).exclude(url='').order_by('-publish_time', '-id')
        if ids:
            qs = qs.filter(id__in=ids)
        if limit > 0:
            qs = qs[:limit]

        scanned = 0
        repaired = 0
        skipped = 0
        failed = 0
        unsafe = 0
        for article in qs:
            scanned += 1
            try:
                old_body = article.content or ''
                if not force and not self._needs_repair(article, old_body):
                    skipped += 1
                    continue

                blocks = self._extract_blocks(article, fetcher=fetcher)
                source = 'fetch'
                if not blocks and not refetch_only:
                    blocks = self._repair_existing_blocks(article)
                    source = 'local'
                body = blocks_to_plain_text(blocks)
                if not body or not self._looks_like_wowhead_article(body):
                    unsafe += 1
                    self.stdout.write(self.style.WARNING(f"unsafe-skip id={article.id} source={source} title={article.title[:80]}"))
                    continue

                update_fields = []
                if article.content != body:
                    article.content = body
                    update_fields.append('content')
                if article.description != body:
                    article.description = body
                    update_fields.append('description')

                blocks_raw = dumps_blocks(blocks)
                if article.content_blocks != blocks_raw:
                    article.content_blocks = blocks_raw
                    update_fields.append('content_blocks')

                content_changed = bool(update_fields)
                if content_changed:
                    cn_blocks = [] if (reset_translation or translate_after) else self._repair_translated_blocks(article.content_blocks_cn or '')
                    cn_body = '' if (reset_translation or translate_after) else self._repair_translated_content(article.content_cn or '')
                    if article.content_cn != cn_body:
                        article.content_cn = cn_body
                        update_fields.append('content_cn')
                    cn_blocks_raw = dumps_blocks(cn_blocks) if cn_blocks else ''
                    if article.content_blocks_cn != cn_blocks_raw:
                        article.content_blocks_cn = cn_blocks_raw
                        update_fields.append('content_blocks_cn')

                if not update_fields:
                    skipped += 1
                    continue

                repaired += 1
                old_stats = self._line_stats(old_body)
                new_stats = self._line_stats(body)
                self.stdout.write(
                    f"repair id={article.id} source={source} lines {old_stats['lines']}->{new_stats['lines']} "
                    f"short {old_stats['short']}->{new_stats['short']} title={article.title[:80]}"
                )
                if apply_changes:
                    with transaction.atomic():
                        article.save(update_fields=sorted(set(update_fields)))
                    if translate_after and translation_service:
                        translation_service.translate_article_fields(article, logger_prefix='repair_wowhead_article_format')
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.WARNING(f"failed id={article.id} error={exc}"))

        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode} done scanned={scanned} repaired={repaired} skipped={skipped} failed={failed} unsafe={unsafe}"
            )
        )

    def _build_wowhead_fetcher(self):
        try:
            from utils.LReq import LReq
            from botend.controller.plugins.wow.wowheadMonitor import wowheadMonitor
            task = MonitorTask.objects.filter(name="wowheadMonitor", is_active=True).first()
            req = LReq(is_chrome=False)
            req.set_current_task(task)
            return wowheadMonitor(req, task)
        except Exception:
            return None

    def _extract_blocks(self, article, fetcher=None):
        try:
            url = article.url
            if fetcher:
                blocks = fetcher._fetch_article_blocks(url, reference_title=article.title or '')
                if blocks:
                    return blocks

            import requests
            resp = requests.get(url, timeout=20, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            if resp.status_code >= 400:
                return []
            blocks = extract_structured_article(resp.text or '', base_url=url, source='wowhead')
            if not blocks:
                return []
            from botend.services.article_content_service import article_blocks_match_reference
            if not article_blocks_match_reference(blocks, reference_title=article.title or '', reference_text=''):
                return []
            return blocks
        except Exception:
            return []

    def _repair_existing_blocks(self, article):
        blocks = loads_blocks(article.content_blocks or '')
        content_blocks = self._text_to_repaired_blocks(article.content or '')
        content_body = blocks_to_plain_text(content_blocks)
        if not blocks:
            return content_blocks if self._looks_like_wowhead_article(content_body) else []

        block_body = blocks_to_plain_text(blocks)
        if not self._looks_like_wowhead_article(block_body):
            return content_blocks if self._looks_like_wowhead_article(content_body) else []

        repaired = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get('type') in TEXT_TYPES:
                for text in self._repair_inline_lines(block.get('text') or ''):
                    new_block = dict(block)
                    new_block['text'] = text
                    repaired.append(new_block)
            else:
                repaired.append(block)
        return repaired or self._text_to_repaired_blocks(article.content or '')

    def _looks_like_wowhead_article(self, text):
        text = (text or '').strip()
        if len(text) < 20:
            return False
        lowered = text.lower()
        bad_markers = [
            '高危漏洞库', '漏洞名称', '阿里云安全专家', 'avd-', 'cve-',
            '关注\n', '下一页 »', '上一页',
        ]
        if any(marker in lowered for marker in bad_markers):
            return False
        ascii_letters = sum(1 for ch in text if ('a' <= ch.lower() <= 'z'))
        cjk_chars = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        return ascii_letters >= 30 and ascii_letters >= cjk_chars

    def _repair_translated_blocks(self, raw_blocks):
        blocks = loads_blocks(raw_blocks or '')
        repaired = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get('type') in TEXT_TYPES:
                for text in self._repair_inline_lines(block.get('text') or ''):
                    new_block = dict(block)
                    new_block['text'] = text
                    repaired.append(new_block)
            else:
                repaired.append(block)
        return repaired

    def _repair_translated_content(self, raw_content):
        if not (raw_content or '').strip():
            return ''
        try:
            pairs = json.loads(raw_content)
        except Exception:
            return '\n'.join(self._repair_inline_lines(raw_content))
        if not isinstance(pairs, list):
            return raw_content
        repaired_pairs = []
        for pair in pairs:
            if isinstance(pair, dict):
                new_pair = dict(pair)
                for key in ('original', 'translated'):
                    if isinstance(new_pair.get(key), str):
                        new_pair[key] = '\n'.join(self._repair_inline_lines(new_pair.get(key) or ''))
                repaired_pairs.append(new_pair)
            elif isinstance(pair, str):
                repaired_pairs.append('\n'.join(self._repair_inline_lines(pair)))
            else:
                repaired_pairs.append(pair)
        return json.dumps(repaired_pairs, ensure_ascii=False)

    def _text_to_repaired_blocks(self, text):
        return [{'type': 'paragraph', 'text': part} for part in self._repair_inline_lines(text)]

    def _repair_inline_lines(self, text):
        lines = [self._clean_inline(line) for line in (text or '').splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            return []

        result = []
        current = ''
        for line in lines:
            if not current:
                current = line
                continue
            if self._should_join(current, line):
                current = self._clean_inline(f'{current} {line}')
            else:
                result.append(current)
                current = line
        if current:
            result.append(current)
        return result

    def _should_join(self, previous, current):
        previous_lower = previous.lower()
        if previous.endswith((',', ';', ':', '(', '[', '/', '—', '-')):
            return True
        if previous_lower.endswith((' and', ' or', ' the', ' a', ' an', ' of', ' to', ' with', ' from', ' in', ' by', ' for')):
            return True
        if current.startswith(('.', ',', ';', ':', ')', ']', '%')):
            return True
        if not re.search(r'[.!?。！？]$', previous) and current[:1].islower():
            return True
        if len(previous) <= 24 and not self._looks_like_heading(previous):
            return True
        return False

    def _looks_like_heading(self, text):
        if len(text) > 80:
            return False
        if re.search(r'[.!?。！？]$', text):
            return False
        words = text.split()
        return bool(words) and sum(1 for word in words if word[:1].isupper()) >= max(1, len(words) // 2)

    def _clean_inline(self, text):
        text = re.sub(r'\s+', ' ', text or '')
        text = re.sub(r'\s+([,.;:!?%)\]}>])', r'\1', text)
        text = re.sub(r'([([{<])\s+', r'\1', text)
        text = re.sub(r'(\d+)\s*/\s*(\d+)', r'\1/\2', text)
        return text.strip()

    def _needs_repair(self, article, body):
        if not (article.content_blocks or '').strip():
            return True
        stats = self._line_stats(body)
        if stats['short'] >= 3:
            return True
        try:
            blocks = json.loads(article.content_blocks or '[]')
        except Exception:
            return True
        if not (len(blocks) == 1 and isinstance(blocks[0], dict) and blocks[0].get('type') == 'html'):
            return True
        text_blocks = [b for b in blocks if isinstance(b, dict) and b.get('type') in TEXT_TYPES]
        if len(text_blocks) == 1 and stats['lines'] > 8:
            return True
        return False

    def _line_stats(self, text):
        lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
        return {
            'lines': len(lines),
            'short': sum(1 for line in lines if len(line) <= 3),
        }
