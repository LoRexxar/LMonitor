import json
import re
import time
import html
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from botend.controller.BaseScan import BaseScan
from botend.models import WowArticle
from core.glm import GLMClient
from utils.log import logger


class PortalArticleTranslateMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.glm = GLMClient()

    def scan(self, url):
        articles = WowArticle.objects.filter(
            source__in=['blizzard_tracker', 'wowhead'],
            category__in=['bluepost', 'news'],
            is_active=True,
            content__isnull=True,
        ).order_by('-publish_time')[:10]

        count = 0
        for article in articles:
            try:
                content = self._fetch_content(article.url, article.source)
                if content:
                    article.content = content
                    article.save()
                    count += 1
                    logger.info(f"[PortalArticleTranslateMonitor] fetched content: {article.title[:50]}")

                title_cn = self._translate_title(article.title)
                if title_cn:
                    article.title_cn = title_cn
                    article.save()

                if content:
                    content_cn = self._translate_content(content)
                    if content_cn:
                        article.content_cn = content_cn
                        article.save()
                        logger.info(f"[PortalArticleTranslateMonitor] translated: {article.title[:50]}")

                time.sleep(1)
            except Exception as e:
                logger.error(f"[PortalArticleTranslateMonitor] error processing {article.url}: {str(e)}")
                continue

        logger.info(f"[PortalArticleTranslateMonitor] processed {count} articles")
        return True

    def _fetch_content(self, url, source):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')

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
            return content
        except Exception as e:
            logger.error(f"[PortalArticleTranslateMonitor] fetch error: {str(e)}")
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
        selectors = [
            '.text',
            '.blog-content',
            'article .content',
            '#blog-post .text',
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

    def _translate_title(self, title):
        if not title:
            return None
        try:
            prompt = f"请将以下英文标题翻译成中文，只返回翻译结果，不要添加任何解释：\n\n{title}"
            result = self.glm.send_message(prompt, max_tokens=200)
            if result:
                return result.strip().strip('"').strip("'")
            return None
        except Exception as e:
            logger.error(f"[PortalArticleTranslateMonitor] translate title error: {str(e)}")
            return None

    def _translate_content(self, content):
        if not content:
            return None
        try:
            paragraphs = [p.strip() for p in content.split('\n') if p.strip()]
            if not paragraphs:
                return None

            translated_pairs = []
            batch_size = 5
            for i in range(0, len(paragraphs), batch_size):
                batch = paragraphs[i:i + batch_size]
                batch_text = '\n'.join(batch)
                prompt = f"""请将以下英文段落翻译成中文。每个段落单独翻译，用换行分隔。只返回翻译结果，不要添加编号或解释。

{batch_text}"""
                result = self.glm.send_message(prompt, max_tokens=2000)
                if result:
                    translated = [t.strip() for t in result.split('\n') if t.strip()]
                    for j, orig in enumerate(batch):
                        trans = translated[j] if j < len(translated) else ''
                        translated_pairs.append({
                            'original': orig,
                            'translated': trans
                        })
                else:
                    for orig in batch:
                        translated_pairs.append({
                            'original': orig,
                            'translated': ''
                        })
                time.sleep(0.5)

            return json.dumps(translated_pairs, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[PortalArticleTranslateMonitor] translate content error: {str(e)}")
            return None
