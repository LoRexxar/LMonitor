#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: ngaMonitor.py
@time: 2024/2/22 16:30
@desc:

'''

import json
import time
import re
import html
import DrissionPage
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

from botend.models import WowArticle
from datetime import datetime
from django.utils import timezone


class wowheadMonitor(BaseScan):
    """
    wowhead监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.post_desp = ""
        self.target_url = "https://www.wowhead.com/wow/retail"
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        cookies = ""

        driver = None
        try:
            driver = self.req.get(self.target_url, 'RespByChrome', 0, cookies, is_origin=1)
        except Exception as e:
            logger.warning("[wowheadMonitor] Chrome request init failed: {}".format(str(e)))

        if not driver or not hasattr(driver, 'eles'):
            logger.error("[wowheadMonitor] Chrome request failed.")
            resp = self.req.getResponse(self.target_url, cookies)
            if resp is False or resp is None:
                logger.error("[wowheadMonitor] Request failed.")
                return False
            status_code = getattr(resp, 'status_code', 200)
            if int(status_code or 0) >= 400:
                logger.error("[wowheadMonitor] Request bad status: {}".format(status_code))
                return False
            return False

        post_count, _ = self.resolve_data(driver, "wowhead", 10)
        if int(post_count or 0) <= 0:
            return False

        return True

    def resolve_data(self, driver, title="", limit=10):

        try:
            time.sleep(2)

            posts_data = []
            for _ in range(8):
                page_html = (getattr(driver, "html", "") or "").strip()
                if "news-card-simple-text-title" in page_html:
                    posts_data = self._parse_posts_from_page_html(page_html, limit=limit)
                    if posts_data:
                        break
                time.sleep(1)

            if not posts_data:
                logger.error("[wowheadMonitor] No posts found.")
                return 0, 0

            new_count = 0
            for post in posts_data[:int(limit or 10)]:
                try:
                    post_type = (post.get("type") or "").strip()
                    post_title = (post.get("title") or "").strip()
                    post_link = (post.get("link") or "").strip()
                    post_preview = (post.get("preview") or "").strip()
                    post_date = (post.get("date") or "").strip()

                    django_date_time = None
                    if post_date:
                        try:
                            original_datetime = datetime.strptime(post_date, "%Y/%m/%d at %I:%M %p")
                            django_date_time = original_datetime
                        except ValueError:
                            try:
                                original_datetime = datetime.strptime(post_date, "%Y/%m/%d at %H:%M")
                                django_date_time = original_datetime
                            except ValueError:
                                django_date_time = None
                        if not django_date_time:
                            try:
                                original_datetime = datetime.strptime(post_date, "%Y/%m/%d at %H:%M %p")
                                django_date_time = original_datetime
                            except ValueError:
                                django_date_time = None
                    if not django_date_time and post_date:
                        try:
                            original_datetime = datetime.strptime(post_date, "%Y/%m/%d at %I:%M%p")
                            django_date_time = original_datetime
                        except ValueError:
                            for fmt in (
                                "%b %d, %Y at %H:%M",
                                "%B %d, %Y at %H:%M",
                                "%b %d, %Y at %I:%M %p",
                                "%B %d, %Y at %I:%M %p",
                            ):
                                try:
                                    original_datetime = datetime.strptime(post_date, fmt)
                                    django_date_time = original_datetime
                                    break
                                except ValueError:
                                    pass
                    if not django_date_time:
                        django_date_time = timezone.localtime(timezone.now()).replace(tzinfo=None)
                    if timezone.is_naive(django_date_time):
                        django_date_time = timezone.make_aware(django_date_time, timezone.get_current_timezone())

                    if not post_link:
                        continue

                    wa = WowArticle.objects.filter(url=post_link).first()
                    if wa:
                        if not (getattr(wa, "description", "") or "").strip() or len((getattr(wa, "description", "") or "")) < 800:
                            body = self._fetch_article_body(post_link, cookies="")
                            if body:
                                wa.description = body
                                # 同步写入 content，供 Portal 详情页与翻译监控使用
                                if not (getattr(wa, "content", "") or "").strip():
                                    wa.content = body
                                    wa.save(update_fields=["description", "content"])
                                else:
                                    wa.save(update_fields=["description"])
                        # 若 content 缺失，补抓正文
                        if not (getattr(wa, "content", "") or "").strip() or len((getattr(wa, "content", "") or "")) < 800:
                            body = self._fetch_article_body(post_link, cookies="")
                            if body:
                                wa.content = body
                                wa.save(update_fields=["content"])
                        continue

                    body = self._fetch_article_body(post_link, cookies="")
                    obj = WowArticle(
                        title="[{}]{}".format(post_type, post_title),
                        url=post_link,
                        publish_time=django_date_time,
                        author="wowhead",
                        description=post_preview or body,
                        content=body or "",
                        source="wowhead",
                        category="news",
                    )
                    obj.save()
                    new_count += 1
                    logger.info("[wowhead Monitor] Found new wowhead article.{}".format(post_title))

                    self.task.flag = post_link
                    self.task.save()

                    self.post_desp = """WowHead新闻<{}>，发帖时间{}
[{}]《{}》
{}""".format(title, post_date, post_type, post_title, post_link)

                    self.trigger_webhook()
                except Exception as e:
                    logger.warning("[wowheadMonitor] Parse post failed: {}".format(str(e)))
                    continue

            return len(posts_data), new_count

        except DrissionPage.errors.ElementNotFoundError:
            logger.error("[wowheadMonitor] bad request.")

        except DrissionPage.errors.PageDisconnectedError:
            logger.error("[wowheadMonitor] PageDisconnectedError.")

        except AttributeError:
            logger.error("[wowheadMonitor] No posts found.")

        except:
            raise

        return 0, 0

    def _parse_posts_from_page_html(self, page_html, limit=10):
        t = page_html or ""
        if not t:
            return []

        cards = []
        title_pat = re.compile(
            r'news-card-simple-text-title"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>',
            flags=re.I,
        )
        for m in title_pat.finditer(t):
            link = html.unescape((m.group(1) or "").strip())
            title_text = re.sub(r'<[^>]+>', '', (m.group(2) or ''), flags=re.I).strip()
            title_text = html.unescape(title_text)
            if link.startswith('/'):
                link = "https://www.wowhead.com{}".format(link)
            if not (title_text and link):
                continue

            start = max(0, int(m.start()) - 800)
            end = min(len(t), int(m.end()) + 1200)
            window = t[start:end]

            type_text = ""
            type_matches = re.findall(r'class="meta-text"[^>]*>([^<]+)</span>', window, flags=re.I)
            if type_matches:
                type_text = html.unescape((type_matches[-1] or "").strip())

            preview_text = ""
            mp = re.search(r'class="[^"]*\bnews-card-simple-text-preview\b[^"]*"[^>]*>([\s\S]*?)</span>', window, flags=re.I)
            if mp:
                preview_text = re.sub(r'<[^>]+>', '', (mp.group(1) or ''), flags=re.I).strip()
                preview_text = html.unescape(preview_text)

            date_text = ""
            md = re.search(r'class="[^"]*\bnews-card-simple-text-byline-posted\b[^"]*"[^>]*title="([^"]+)"', window, flags=re.I)
            if md:
                date_text = html.unescape((md.group(1) or "").strip())

            cards.append(
                {
                    "type": type_text,
                    "title": title_text,
                    "link": link,
                    "preview": preview_text,
                    "date": date_text,
                }
            )

            if len(cards) >= int(limit or 10):
                break

        return cards

    def _fetch_article_body(self, url, cookies=""):
        try:
            html_text = ""
            # Wowhead 部分页面正文可能需要前端渲染，优先尝试 Chrome 渲染版本
            try:
                if self.req and getattr(self.req, 'is_chrome', False):
                    driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)
                    if driver and hasattr(driver, 'html'):
                        html_text = (getattr(driver, 'html', '') or '').strip()
            except Exception:
                html_text = html_text or ""
            if not html_text:
                resp = self.req.getResponse(url, cookies)
                if not resp:
                    return ""
                status_code = getattr(resp, 'status_code', 200)
                if int(status_code or 0) >= 400:
                    return ""
                html_text = getattr(resp, 'text', '') or ''
            if not html_text:
                return ""
            return self._extract_body_from_html(html_text)
        except Exception:
            return ""

    def _extract_body_from_html(self, html_text):
        t = html_text or ""
        blocks = []
        for pat in (
            r'<div[^>]+class="[^"]*(?:news-post-content|news-post-text|content-body)[^"]*"[^>]*>([\s\S]*?)</div>',
            r'<article[^>]*>([\s\S]*?)</article>',
        ):
            m = re.search(pat, t, flags=re.I)
            if m:
                blocks.append(m.group(1) or "")
        if not blocks:
            return ""
        raw = max(blocks, key=lambda x: len(x or ""))
        raw = re.sub(r'<(script|style|noscript)[^>]*>[\s\S]*?</\1>', '', raw, flags=re.I)
        raw = re.sub(r'(?i)<br\s*/?>', '\n', raw)
        raw = re.sub(r'(?i)</p\s*>', '\n\n', raw)
        raw = re.sub(r'(?i)</div\s*>', '\n\n', raw)
        raw = re.sub(r'(?i)</li\s*>', '\n', raw)
        raw = re.sub(r'<[^>]+>', '', raw)
        raw = html.unescape(raw)
        raw = raw.replace('\r\n', '\n').replace('\r', '\n')
        lines = [ln.strip() for ln in raw.split('\n')]
        out = []
        blank = 0
        for ln in lines:
            if not ln:
                blank += 1
                if blank <= 1:
                    out.append("")
                continue
            blank = 0
            out.append(ln)
        text = "\n".join(out).strip()
        # 兜底：正则没抓到/抓到的太短时，用 BS4 再试一轮
        if len(text) >= 400:
            return text
        try:
            from bs4 import BeautifulSoup
        except Exception:
            return text
        try:
            soup = BeautifulSoup(t, 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
                tag.decompose()
            # JSON-LD articleBody（如果有）
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
                '#blog-post .text',
                '#news-post .text',
                '.news-post .text',
                '.blog-post .text',
                '.article-content',
                '.post-content',
                '.news-post-content',
                '.news-post-text',
                '.content-body',
                '.text',
                'article',
                'main',
            ]
            best = ""
            for sel in selectors:
                el = soup.select_one(sel)
                if not el:
                    continue
                cand = el.get_text(separator='\n', strip=True)
                if cand and len(cand) > len(best):
                    best = cand
            best = (best or "").strip()
            return best if len(best) > len(text) else text
        except Exception:
            return text

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.send_msg(self.post_desp)
