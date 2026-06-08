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
from core.glm import GLMClient
from utils.log import logger


class PortalArticleTranslateMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.glm = GLMClient()

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
                    content = self._fetch_content(article.url, article.source)
                    if content:
                        article.content = content
                        article.save()
                        fetched_count += 1
                        logger.info(f"[PortalArticleTranslateMonitor] fetched content: {article.title[:50]}")

                if article.title and not article.title_cn:
                    title_cn = self._translate_title(article.title)
                    if title_cn:
                        article.title_cn = title_cn
                        article.save()

                if article.content and not article.content_cn:
                    content_cn = self._translate_content(article.content)
                    if content_cn:
                        article.content_cn = content_cn
                        article.save()
                        translated_count += 1
                        logger.info(f"[PortalArticleTranslateMonitor] translated: {article.title[:50]}")

                time.sleep(1)
            except Exception as e:
                logger.error(f"[PortalArticleTranslateMonitor] error processing {article.url}: {str(e)}")
                continue

        logger.info(f"[PortalArticleTranslateMonitor] fetched {fetched_count}, translated {translated_count}")
        return True

    def _fetch_content(self, url, source):
        try:
            html_text = self._fetch_html(url, source)
            if not html_text:
                return None

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
            return content
        except Exception as e:
            logger.error(f"[PortalArticleTranslateMonitor] fetch error: {str(e)}")
            return None

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

    def _translate_title(self, title):
        if not title:
            return None
        try:
            prompt = f"请将以下英文标题翻译成中文，只返回翻译结果，不要添加任何解释：\n\n{title}"
            result = self.glm.send_message(prompt, max_tokens=200, thinking_type="disabled")
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
            paragraphs = [p.strip() for p in content.split('\n') if p and p.strip()]
            if not paragraphs:
                return None

            translated_pairs = []
            # 分段策略：按字符长度组 batch，避免单次过长导致模型截断/输出不齐
            i = 0
            while i < len(paragraphs):
                batch = []
                total = 0
                while i < len(paragraphs) and len(batch) < 10:
                    p = paragraphs[i]
                    # 单段过长就直接截断，避免超长段落影响整批
                    if len(p) > 2000:
                        p = p[:2000]
                    if batch and (total + len(p) > 4000):
                        break
                    batch.append(p)
                    total += len(p)
                    i += 1

                prompt = (
                    "请把下面 JSON 数组中的每个英文字符串翻译成中文，保持数组长度与顺序一致。"
                    "仅输出 JSON 数组（不要输出其它文字/解释/Markdown）。\n\n"
                    f"输入JSON：\n{json.dumps(batch, ensure_ascii=False)}"
                )
                result = self.glm.send_message(prompt, max_tokens=2400, thinking_type="disabled")
                translated_list = None
                if result:
                    try:
                        translated_list = json.loads(result)
                    except Exception:
                        translated_list = None
                if not isinstance(translated_list, list):
                    # 兜底：按行对齐（不可靠，但保证不中断）
                    translated_list = [t.strip() for t in (result or '').splitlines() if t.strip()]

                for j, orig in enumerate(batch):
                    trans = ''
                    if j < len(translated_list) and isinstance(translated_list[j], str):
                        trans = translated_list[j].strip()
                    translated_pairs.append({'original': orig, 'translated': trans})

                time.sleep(0.6)

            return json.dumps(translated_pairs, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[PortalArticleTranslateMonitor] translate content error: {str(e)}; glm_error={getattr(self.glm, 'last_error', '')}")
            return None
