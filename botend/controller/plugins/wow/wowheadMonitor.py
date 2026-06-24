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
import os
import tempfile
from hashlib import sha1
from urllib.parse import urlparse
import DrissionPage
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface
from botend.services.article_translation_service import build_translation_service
from botend.services.article_content_service import article_blocks_match_reference, blocks_to_plain_text, dumps_blocks, extract_structured_article, plain_text_to_blocks

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
        self.translation_service = build_translation_service()
        self.last_error_detail = ""
        # 单次 scan 最多翻译多少篇（包括补翻译历史记录）
        self._translate_budget = 10

    def _set_error_detail(self, message, **extra):
        parts = [str(message or "").strip()]
        for key, value in extra.items():
            if value is None:
                continue
            parts.append("{}={}".format(key, value))
        self.last_error_detail = "; ".join([p for p in parts if p])[:1000]
        return self.last_error_detail

    def _html_debug_summary(self, html_text, prefix="html"):
        text = html_text or ""
        low = text.lower()
        return {
            "{}_len".format(prefix): len(text),
            "{}_news_data".format(prefix): "data.home.newsData" in text,
            "{}_news_href_count".format(prefix): low.count("/news/"),
            "{}_cloudflare".format(prefix): "cloudflare" in low or "cf-chl" in low,
            "{}_captcha".format(prefix): "captcha" in low,
            "{}_access_denied".format(prefix): "access denied" in low,
            "{}_just_moment".format(prefix): "just a moment" in low,
        }

    def _request_fallback_posts(self, cookies, limit=10, reason=""):
        try:
            resp = self.req.get(self.target_url, 'Response', 0, cookies)
        except Exception as e:
            self._set_error_detail("wowhead requests fallback exception", reason=reason, error=str(e))
            return 0, 0, False
        if resp is False or resp is None:
            self._set_error_detail("wowhead requests fallback returned empty", reason=reason)
            return 0, 0, False
        status_code = getattr(resp, 'status_code', 200)
        html_text = getattr(resp, "text", "") or ""
        if int(status_code or 0) >= 400:
            detail = self._html_debug_summary(html_text, prefix="response")
            detail.update({"reason": reason, "status": status_code})
            self._set_error_detail("wowhead requests fallback bad status", **detail)
            return 0, 0, False
        fake_driver = type("RespWrapper", (), {"html": html_text, "eles": True})()
        post_count, new_count = self.resolve_data(fake_driver, "wowhead", limit)
        if int(post_count or 0) <= 0:
            detail = self._html_debug_summary(html_text, prefix="response")
            detail.update({"reason": reason, "status": status_code})
            self._set_error_detail("wowhead requests fallback parsed no posts", **detail)
            return post_count, new_count, False
        return post_count, new_count, True

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

        # Chrome 不可用时，尝试 Cloak/Playwright 渲染
        if not driver or not hasattr(driver, 'html'):
            try:
                driver = self.req.get(self.target_url, 'RespByCloak', 0, cookies, is_origin=1)
            except Exception as e:
                logger.warning("[wowheadMonitor] Cloak request init failed: {}".format(str(e)))

        if not driver or not hasattr(driver, 'eles'):
            logger.error("[wowheadMonitor] Browser request failed.")
            # 最后再尝试 requests 直连 HTML，哪怕不稳定，也至少保留退路
            _, _, ok = self._request_fallback_posts(cookies, limit=10, reason="browser_unavailable")
            return ok

        post_count, _ = self.resolve_data(driver, "wowhead", 10)
        if int(post_count or 0) <= 0:
            html_text = (getattr(driver, "html", "") or "")
            detail = self._html_debug_summary(html_text, prefix="browser")
            self._set_error_detail("wowhead browser parsed no posts", **detail)
            logger.warning("[wowheadMonitor] Browser parsed no posts, fallback to requests. {}".format(self.last_error_detail))
            _, _, ok = self._request_fallback_posts(cookies, limit=10, reason="browser_parsed_no_posts")
            return ok

        return True

    def resolve_data(self, driver, title="", limit=10):

        try:
            time.sleep(2)

            posts_data = []
            for _ in range(8):
                page_html = (getattr(driver, "html", "") or "").strip()
                if page_html:
                    posts_data = self._parse_posts_from_page_html(page_html, limit=limit)
                    if posts_data:
                        break
                time.sleep(1)

            if not posts_data:
                logger.error("[wowheadMonitor] No posts found.")
                return 0, 0

            new_count = 0
            translated_count = 0
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
                        existing_blocks = []
                        try:
                            existing_blocks = json.loads(getattr(wa, "content_blocks", "") or "[]")
                        except Exception:
                            existing_blocks = []
                        needs_image_blocks = not any(isinstance(b, dict) and b.get("type") == "image" for b in (existing_blocks or []))
                        needs_image_upload = self._needs_article_image_upload(existing_blocks)
                        if needs_image_upload and not needs_image_blocks:
                            uploaded_blocks = self._upload_article_images(existing_blocks, article_url=post_link)
                            if uploaded_blocks != existing_blocks:
                                wa.content_blocks = dumps_blocks(uploaded_blocks)
                                wa.save(update_fields=["content_blocks"])
                                existing_blocks = uploaded_blocks
                                needs_image_upload = self._needs_article_image_upload(existing_blocks)
                        if not (getattr(wa, "description", "") or "").strip() or len((getattr(wa, "description", "") or "")) < 800 or needs_image_blocks or needs_image_upload:
                            blocks = self._fetch_article_blocks(post_link, cookies="", reference_text=wa.content or wa.description or "", reference_title=wa.title or "")
                            body = blocks_to_plain_text(blocks)
                            if body:
                                wa.description = body
                                update_fields = ["description"]
                                if blocks and (needs_image_blocks or needs_image_upload or not (getattr(wa, "content_blocks", "") or "").strip()):
                                    wa.content_blocks = dumps_blocks(blocks)
                                    update_fields.append("content_blocks")
                                # 同步写入 content，供 Portal 详情页与翻译监控使用
                                if not (getattr(wa, "content", "") or "").strip():
                                    wa.content = body
                                    update_fields.append("content")
                                wa.save(update_fields=update_fields)
                        # 若 content 缺失，补抓正文
                        if not (getattr(wa, "content", "") or "").strip() or len((getattr(wa, "content", "") or "")) < 800:
                            blocks = self._fetch_article_blocks(post_link, cookies="", reference_text=wa.content or wa.description or "", reference_title=wa.title or "")
                            body = blocks_to_plain_text(blocks)
                            if body:
                                wa.content = body
                                update_fields = ["content"]
                                if blocks and (needs_image_blocks or not (getattr(wa, "content_blocks", "") or "").strip()):
                                    wa.content_blocks = dumps_blocks(blocks)
                                    update_fields.append("content_blocks")
                                wa.save(update_fields=update_fields)
                        # 若译文缺失，补翻译（标题/内容）
                        if translated_count < self._translate_budget:
                            translated = self._ensure_translated(wa)
                            if translated:
                                translated_count += 1
                        continue

                    blocks = self._fetch_article_blocks(post_link, cookies="", reference_title=post_title)
                    body = blocks_to_plain_text(blocks)
                    obj = WowArticle(
                        title="[{}]{}".format(post_type, post_title),
                        url=post_link,
                        publish_time=django_date_time,
                        author="wowhead",
                        description=post_preview or body,
                        content=body or "",
                        content_blocks=dumps_blocks(blocks) if blocks else "",
                        source="wowhead",
                        category="news",
                    )
                    obj.save()
                    new_count += 1
                    logger.info("[wowhead Monitor] Found new wowhead article.{}".format(post_title))

                    if translated_count < self._translate_budget:
                        translated = self._ensure_translated(obj)
                        if translated:
                            translated_count += 1

                    self.task.flag = post_link
                    self.task.save()

                    self.post_desp = """WowHead新闻<{}>，发帖时间{}
[{}]《{}》
{}""".format(title, post_date, post_type, post_title, post_link)

                    self.trigger_webhook()
                except Exception as e:
                    logger.warning("[wowheadMonitor] Parse post failed: {}".format(str(e)))
                    continue

            # 补翻译：避免只翻译“最新列表里出现的文章”，导致历史文章长期 title_cn/content_cn 为空
            try:
                if translated_count < self._translate_budget and self.translation_service.available():
                    from django.db.models import Q
                    missing = (
                        WowArticle.objects.filter(source="wowhead")
                        .filter(Q(title_cn__isnull=True) | Q(title_cn="") | Q(content_cn__isnull=True) | Q(content_cn=""))
                        .exclude(content="")
                        .order_by("-publish_time")[: max(0, self._translate_budget - translated_count)]
                    )
                    for wa2 in missing:
                        if translated_count >= self._translate_budget:
                            break
                        if self._ensure_translated(wa2):
                            translated_count += 1
            except Exception:
                pass

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

    def _ensure_translated(self, article: WowArticle) -> bool:
        """
        端到端链路要求：
        1) 先抓取并落库原文（title/content）
        2) 标题与正文分段走 GLM 翻译
        3) 保存 title_cn/content_cn（分开保存，正文失败不影响标题）
        4) 若本次抓取/翻译失败，下一次 scan 会继续补齐
        """
        return self.translation_service.translate_article_fields(
            article,
            logger_prefix="wowheadMonitor",
        )

    def _parse_posts_from_page_html(self, page_html, limit=10):
        t = page_html or ""
        if not t:
            return []

        try:
            from bs4 import BeautifulSoup
        except Exception:
            BeautifulSoup = None

        cards = []
        seen = set()

        if BeautifulSoup:
            try:
                soup = BeautifulSoup(t, "html.parser")
                json_cards = self._parse_posts_from_home_news_json(soup, limit=limit)
                if json_cards:
                    return json_cards

                anchors = soup.select(
                    "a.recent-news-post-list-topic, "
                    ".news-card-simple-text-title a, "
                    "a[href*='/news/']"
                )
                for a in anchors:
                    href = (a.get("href") or "").strip()
                    if not href or "/news/" not in href:
                        continue
                    if href.startswith("/"):
                        href = "https://www.wowhead.com{}".format(href)
                    # wowheadMonitor 只处理 wowhead news，不处理 blue-tracker
                    if "/blue-tracker/" in href:
                        continue
                    if href in seen:
                        continue

                    title_text = " ".join((a.get_text(" ", strip=True) or "").split())
                    if not title_text or len(title_text) < 8:
                        continue

                    container = a
                    for _ in range(4):
                        if not getattr(container, "parent", None):
                            break
                        container = container.parent
                    window_text = " ".join((container.get_text(" ", strip=True) or "").split())

                    type_text = ""
                    if "ptr" in title_text.lower() or " ptr " in window_text.lower():
                        type_text = "PTR"
                    elif "live" in str(" ".join(a.get("class") or [])).lower() or re.search(r"\b\d+[hd]\s+ago\b", window_text.lower()):
                        type_text = "Live"

                    preview_text = ""
                    preview_node = None
                    for sel in [
                        ".news-card-simple-text-preview",
                        ".recent-news-post-list-preview",
                        ".listview-row-abstract",
                        "p",
                        "span",
                    ]:
                        preview_node = container.select_one(sel) if hasattr(container, "select_one") else None
                        if preview_node:
                            preview_text = " ".join((preview_node.get_text(" ", strip=True) or "").split())
                            if preview_text and preview_text != title_text:
                                break
                    if preview_text == title_text:
                        preview_text = ""

                    date_text = ""
                    m_ago = re.search(r"(\d+\s*[hd]\s+ago)", window_text.lower())
                    if m_ago:
                        date_text = m_ago.group(1)

                    cards.append(
                        {
                            "type": type_text,
                            "title": html.unescape(title_text),
                            "link": href,
                            "preview": html.unescape(preview_text),
                            "date": html.unescape(date_text),
                        }
                    )
                    seen.add(href)
                    if len(cards) >= int(limit or 10):
                        break
                if cards:
                    return cards
            except Exception:
                pass

        # 正则兜底，兼容旧结构
        title_pat = re.compile(
            r'(?:recent-news-post-list-topic|news-card-simple-text-title"[^>]*>\s*<a|<a[^>]*class="[^"]*recent-news-post-list-topic[^"]*"[^>]*href=")([^"]+)"[^>]*>([\s\S]*?)</a>',
            flags=re.I,
        )
        for m in title_pat.finditer(t):
            link = html.unescape((m.group(1) or "").strip())
            title_text = re.sub(r'<[^>]+>', '', (m.group(2) or ''), flags=re.I).strip()
            title_text = html.unescape(title_text)
            if link.startswith('/'):
                link = "https://www.wowhead.com{}".format(link)
            if not (title_text and link) or '/news/' not in link or '/blue-tracker/' in link or link in seen:
                continue
            cards.append({"type": "", "title": title_text, "link": link, "preview": "", "date": ""})
            seen.add(link)
            if len(cards) >= int(limit or 10):
                break
        return cards

    def _parse_posts_from_home_news_json(self, soup, limit=10):
        """解析 Wowhead 首页当前的 JSON 数据块。"""
        try:
            script = soup.select_one('script#data\.home\.newsData')
            if not script:
                script = soup.find('script', attrs={'id': 'data.home.newsData'})
            raw = (script.string or script.get_text() or '').strip() if script else ''
            if not raw:
                return []
            data = json.loads(raw)
        except Exception:
            return []

        posts = data.get('newsPosts') if isinstance(data, dict) else None
        if not isinstance(posts, list):
            return []

        cards = []
        seen = set()
        for item in posts:
            if not isinstance(item, dict):
                continue
            href = (item.get('postUrl') or item.get('url') or '').strip()
            if not href or '/news/' not in href or '/blue-tracker/' in href:
                continue
            if href.startswith('/'):
                href = "https://www.wowhead.com{}".format(href)
            if href in seen:
                continue

            title_text = " ".join(str(item.get('title') or '').split())
            if not title_text or len(title_text) < 8:
                continue
            preview_text = " ".join(str(item.get('preview') or '').split())
            type_text = " ".join(str(item.get('typeName') or '').split())
            date_text = " ".join(str(item.get('postedFull') or item.get('posted') or '').split())

            cards.append({
                "type": html.unescape(type_text),
                "title": html.unescape(title_text),
                "link": href,
                "preview": html.unescape(preview_text),
                "date": html.unescape(date_text),
            })
            seen.add(href)
            if len(cards) >= int(limit or 10):
                break
        return cards

    def _fetch_article_body(self, url, cookies=""):
        blocks = self._fetch_article_blocks(url, cookies=cookies)
        return blocks_to_plain_text(blocks)

    def _fetch_article_blocks(self, url, cookies="", reference_text="", reference_title=""):
        try:
            html_text = self._fetch_article_html(url, cookies=cookies)
            if not html_text:
                return []
            blocks = extract_structured_article(html_text, base_url=url, source="wowhead")
            if blocks:
                uploaded_blocks = self._upload_article_images(blocks, article_url=url)
                candidate_blocks = uploaded_blocks or blocks
                if article_blocks_match_reference(candidate_blocks, reference_text=reference_text, reference_title=reference_title):
                    return candidate_blocks
                logger.warning("[wowheadMonitor] Skip unsafe article blocks for {}".format(url))
                return []
            body = self._extract_body_from_html(html_text)
            fallback_blocks = plain_text_to_blocks(body)
            if fallback_blocks and article_blocks_match_reference(fallback_blocks, reference_text=reference_text, reference_title=reference_title):
                return fallback_blocks
            return []
        except Exception:
            return []

    def _upload_article_images(self, blocks, article_url=""):
        result = []
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            new_block = dict(block)
            if self._needs_article_image_upload([new_block]):
                uploaded_url = self._download_and_upload_image(new_block.get("url"), article_url=article_url)
                if uploaded_url:
                    new_block["source_url"] = new_block.get("url") or ""
                    new_block["url"] = uploaded_url
            result.append(new_block)
        return result

    def _needs_article_image_upload(self, blocks):
        for block in blocks or []:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            image_url = (block.get("url") or "").strip()
            if image_url and self._is_external_article_image_url(image_url):
                return True
        return False

    def _is_external_article_image_url(self, image_url):
        image_url = (image_url or "").strip()
        if not image_url.startswith(("http://", "https://")):
            return False
        try:
            from django.conf import settings
            base_url = ((getattr(settings, "OSS_CONFIG", {}) or {}).get("base_url") or "").strip()
            if base_url and image_url.startswith(base_url.rstrip("/") + "/"):
                return False
        except Exception:
            pass
        return True

    def _download_and_upload_image(self, image_url, article_url=""):
        image_url = (image_url or "").strip()
        if not image_url or not image_url.startswith(("http://", "https://")):
            return ""
        try:
            from botend.interface.ossupload import ossUploadObject
            resp = self.req.get(image_url, "Response", 0, "") if self.req else None
            if not resp:
                return ""
            status_code = getattr(resp, "status_code", 200)
            if int(status_code or 0) >= 400:
                return ""
            content = getattr(resp, "content", None)
            if content is None:
                text = getattr(resp, "text", "") or ""
                content = text.encode("utf-8")
            if not content:
                return ""
            suffix = self._image_suffix(image_url, getattr(resp, "headers", {}) or {})
            digest = sha1(image_url.encode("utf-8")).hexdigest()[:16]
            article_slug = self._article_slug(article_url)
            object_key = "portal/wowhead/{}/{}{}".format(article_slug, digest, suffix)
            tmp_path = ""
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                return ossUploadObject(tmp_path, object_key=object_key) or ""
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        except Exception as e:
            logger.warning("[wowheadMonitor] Upload article image failed {}: {}".format(image_url, str(e)))
            return ""

    def _image_suffix(self, image_url, headers):
        content_type = ""
        try:
            content_type = (headers.get("Content-Type") or headers.get("content-type") or "").split(";")[0].strip().lower()
        except Exception:
            content_type = ""
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        if content_type in mapping:
            return mapping[content_type]
        path = urlparse(image_url).path.lower()
        _, ext = os.path.splitext(path)
        if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return ".jpg" if ext == ".jpeg" else ext
        return ".jpg"

    def _article_slug(self, article_url):
        path = urlparse(article_url or "").path.strip("/")
        slug = path.split("/")[-1] if path else "article"
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-")
        return slug[:96] or "article"

    def _fetch_article_html(self, url, cookies=""):
        try:
            html_text = ""
            # Wowhead 部分页面正文可能需要前端渲染，优先尝试 Chrome 渲染版本
            try:
                if self.req and getattr(self.req, 'is_chrome', False):
                    driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)
                    if driver and hasattr(driver, 'html'):
                        # 等待正文相关 DOM 出现（否则 html 可能只有骨架）
                        for _ in range(8):
                            html_text = (getattr(driver, 'html', '') or '').strip()
                            if ('news-post-content' in html_text) or ('article-content' in html_text) or ('application/ld+json' in html_text):
                                break
                            time.sleep(0.8)
            except Exception:
                html_text = html_text or ""

            # Chrome 不可用时，尝试 Cloak(Playwright) 渲染（比 requests 更稳，且能绕过部分 403）
            try:
                if not html_text and self.req and getattr(self.req, 'is_cloak', False):
                    driver = self.req.get(url, 'RespByCloak', 0, cookies, is_origin=1)
                    if driver and hasattr(driver, 'html'):
                        for _ in range(8):
                            html_text = (getattr(driver, 'html', '') or '').strip()
                            if ('news-post-content' in html_text) or ('article-content' in html_text) or ('application/ld+json' in html_text):
                                break
                            time.sleep(0.8)
            except Exception:
                html_text = html_text or ""

            if not html_text:
                # 使用带重试的封装（避免偶发超时导致正文长期为空）
                resp = self.req.get(url, "Response", 0, cookies)
                if not resp:
                    return ""
                status_code = getattr(resp, 'status_code', 200)
                if int(status_code or 0) >= 400:
                    return ""
                html_text = getattr(resp, 'text', '') or ''
            if not html_text:
                return ""
            return html_text
        except Exception:
            return ""

    def _extract_body_from_html(self, html_text):
        t = html_text or ""
        try:
            from bs4 import BeautifulSoup
        except Exception:
            # 没有 bs4 的话只能做非常粗糙的兜底
            m = re.search(r'<article[^>]*>([\s\S]*?)</article>', t, flags=re.I)
            if not m:
                return ""
            raw = re.sub(r'<[^>]+>', '', m.group(1) or '')
            return html.unescape(raw).strip()

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
                'div.news-post-content',
                'div.news-post-text',
                'div.content-body',
                '.article-content',
                '.post-content',
                '.text',
                'article',
                'main',
            ]
            best = ""
            for sel in selectors:
                el = soup.select_one(sel)
                if not el:
                    continue
                # wowhead 的正文 div 是嵌套结构，用 BS4 取 text 才不会被 </div> 截断
                cand = el.get_text(separator='\n', strip=True)
                if cand and len(cand) > len(best):
                    best = cand
            best = (best or "").strip()
            return best
        except Exception:
            return ""

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.send_msg(self.post_desp)
