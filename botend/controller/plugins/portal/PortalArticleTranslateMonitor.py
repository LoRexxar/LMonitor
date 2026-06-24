import json
import re
import time
import html
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from django.db.models import Q

from botend.controller.BaseScan import BaseScan
from botend.models import WowArticle
from botend.services.article_content_service import article_blocks_match_reference, blocks_to_plain_text, dumps_blocks, extract_structured_article, plain_text_to_blocks
from botend.services.article_translation_service import build_translation_service
from utils.log import logger


class PortalArticleTranslateMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.translation_service = build_translation_service()

    def scan(self, url):
        query = Q(
            source__in=['blizzard_tracker', 'wowhead'],
            category__in=['bluepost', 'news'],
            is_active=True,
            url__isnull=False,
        ) & ~Q(url='') & (
            Q(content__isnull=True) | Q(content='') |
            Q(title_cn__isnull=True) | Q(title_cn='') |
            Q(content_cn__isnull=True) | Q(content_cn='')
        )

        articles = WowArticle.objects.filter(query).order_by('-publish_time')[:10]

        fetched_count = 0
        translated_count = 0
        for article in articles:
            try:
                if not article.content:
                    blocks = self._fetch_content_blocks(
                        article.url,
                        article.source,
                        reference_text=article.content or article.description or "",
                        reference_title=article.title or "",
                    )
                    content = blocks_to_plain_text(blocks)
                    if not content:
                        content = self._fetch_content(
                            article.url,
                            article.source,
                            reference_text=article.content or article.description or "",
                            reference_title=article.title or "",
                        )
                        blocks = plain_text_to_blocks(content)
                    if content:
                        article.content = content
                        update_fields = ["content"]
                        if blocks and not (article.content_blocks or "").strip():
                            article.content_blocks = dumps_blocks(blocks)
                            update_fields.append("content_blocks")
                        article.save(update_fields=update_fields)
                        fetched_count += 1
                        logger.info(f"[PortalArticleTranslateMonitor] fetched content: {article.title[:50]}")

                had_content_cn = bool((article.content_cn or "").strip())
                did_translate = self.translation_service.translate_article_fields(
                    article,
                    logger_prefix="PortalArticleTranslateMonitor",
                )
                if did_translate and not had_content_cn and (article.content_cn or "").strip():
                    translated_count += 1
                    logger.info(f"[PortalArticleTranslateMonitor] translated: {article.title[:50]}")

                time.sleep(1)
            except Exception as e:
                logger.error(f"[PortalArticleTranslateMonitor] error processing {article.url}: {str(e)}")
                continue

        logger.info(f"[PortalArticleTranslateMonitor] fetched {fetched_count}, translated {translated_count}")
        return True

    def _fetch_content(self, url, source, reference_text="", reference_title=""):
        blocks = self._fetch_content_blocks(url, source, reference_text=reference_text, reference_title=reference_title)
        if blocks:
            return blocks_to_plain_text(blocks)
        return None

    def _fetch_content_blocks(self, url, source, reference_text="", reference_title=""):
        try:
            html_text = self._fetch_html(url, source)
            if not html_text:
                return []

            blocks = extract_structured_article(html_text, base_url=url, source=source)
            if blocks:
                if article_blocks_match_reference(blocks, reference_text=reference_text, reference_title=reference_title):
                    return blocks
                logger.warning(f"[PortalArticleTranslateMonitor] skip unsafe content blocks: {url}")
                return []

            soup = BeautifulSoup(html_text, 'html.parser')

            for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
                tag.decompose()

            content = None
            if source == 'blizzard_tracker':
                content = self._extract_blizzard_content(soup)
            elif source == 'wowhead':
                content = self._extract_wowhead_content(soup)
            else:
                content = self._extract_generic_content(soup)

            if content:
                content = self._clean_content(content)
            fallback_blocks = plain_text_to_blocks(content or "")
            if fallback_blocks and article_blocks_match_reference(fallback_blocks, reference_text=reference_text, reference_title=reference_title):
                return fallback_blocks
            if fallback_blocks:
                logger.warning(f"[PortalArticleTranslateMonitor] skip unsafe fallback content: {url}")
            return []
        except Exception as e:
            logger.error(f"[PortalArticleTranslateMonitor] fetch error: {str(e)}")
            return []

    def _fetch_html(self, url, source):
        """
        优先使用 LReq（可选 Chrome/代理/重试），避免 requests 直接拉取在某些站点被拦截/拿到不完整 HTML。
        """
        url = (url or '').strip()
        if not url:
            return None
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36'
        }
        try:
            # wowhead 某些页面内容依赖前端渲染，优先尝试 Chrome 渲染版本
            if source == 'wowhead' and self.req and getattr(self.req, 'is_chrome', False):
                driver = None
                try:
                    driver = self.req.get(url, 'RespByChrome', 0, '', is_origin=1)
                except Exception:
                    driver = None
                if driver and hasattr(driver, 'html'):
                    t = (getattr(driver, 'html', '') or '').strip()
                    if t:
                        return t
        except Exception:
            pass

        # 通用：走 LReq 的 Response（含重试/代理等）
        try:
            if self.req and hasattr(self.req, 'getResponse'):
                resp = self.req.getResponse(url, '', headers=headers)
                if resp and getattr(resp, 'status_code', 0) == 200:
                    t = (getattr(resp, 'text', '') or '').strip()
                    if t:
                        return t
        except Exception:
            pass

        # 兜底：requests 直接抓
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                return None
            return (resp.text or '').strip() or None
        except Exception:
            return None

    def _extract_blizzard_content(self, soup):
        selectors = [
            '.topic-body .post-content',
            '.TopicPost-bodyContent',
            '.post-content',
            'article .content',
            '.topic-content',
        ]
        for selector in selectors:
            el = soup.select_one(selector)
            if el:
                return el.get_text(separator='\n', strip=True)

        main = soup.find('main') or soup.find('article')
        if main:
            return main.get_text(separator='\n', strip=True)
        return None

    def _extract_wowhead_content(self, soup):
        # 1) 优先尝试 JSON-LD 中的 articleBody（通常最干净）
        try:
            for sc in soup.find_all('script', attrs={'type': 'application/ld+json'}):
                raw = (sc.string or sc.get_text() or '').strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                candidates = []
                if isinstance(obj, dict):
                    candidates.append(obj)
                elif isinstance(obj, list):
                    candidates.extend([x for x in obj if isinstance(x, dict)])
                for it in candidates:
                    body = (it.get('articleBody') or it.get('text') or '').strip()
                    if body and len(body) > 200:
                        return body
        except Exception:
            pass

        selectors = [
            # Wowhead 文章正文常见容器
            '#blog-post .text',
            '#news-post .text',
            '.news-post .text',
            '.blog-post .text',
            '.article-content',
            '.post-content',
            '.news-post-content',
            '.news-post-text',
            '.content-body',
            # 兜底：页面上可能存在多个 .text，放最后再尝试
            '.text',
            '.blog-content',
            'article .content',
            '.news-content',
        ]
        for selector in selectors:
            el = soup.select_one(selector)
            if el:
                return el.get_text(separator='\n', strip=True)

        main = soup.find('main') or soup.find('article')
        if main:
            return main.get_text(separator='\n', strip=True)
        return None

    def _extract_generic_content(self, soup):
        main = soup.find('main') or soup.find('article') or soup.find('div', class_='content')
        if main:
            return main.get_text(separator='\n', strip=True)
        return soup.get_text(separator='\n', strip=True)[:5000]

    def _clean_content(self, text):
        if not text:
            return None
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        lines = [line.strip() for line in text.split('\n')]
        lines = [line for line in lines if line]
        return '\n'.join(lines)
