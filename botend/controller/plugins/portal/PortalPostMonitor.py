import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
import email.utils
import datetime
from urllib.parse import urljoin, urlparse

from botend.controller.BaseScan import BaseScan
from botend.models import TargetAuth, WowArticle
from botend.alerting import upsert_system_alert
from botend.services.article_image_service import upload_article_images_in_blocks
from django.utils import timezone
from utils.log import logger
from botend.services.article_content_service import blocks_to_plain_text, dumps_blocks, extract_structured_article, plain_text_to_blocks
from botend.services.article_translation_service import build_translation_service


def _hash_url(url):
    u = (url or '').strip()
    if not u:
        return None
    return hashlib.sha256(u.encode('utf-8')).hexdigest()


class PortalPostMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.translation_service = build_translation_service()
        self._translate_budget = 3

    @staticmethod
    def _normalize_nga_url(url):
        url = str(url or '').strip()
        if url.startswith('https://nga.178.com/'):
            return 'https://bbs.nga.cn/' + url[len('https://nga.178.com/'):]
        return urljoin('https://bbs.nga.cn/', url)

    def scan(self, url):
        self.update_blueposts()
        self.update_exwind_latest()
        self.update_blizzard_cn_news()
        self.update_nga_hot()
        return True

    def _upsert_article(self, *, title, url, source, category, author=None, description=None, publish_time=None, reply_count=None):
        url = (url or '').strip()
        if not url:
            return
        url_hash = _hash_url(url)
        existing = WowArticle.objects.filter(url=url).only("id", "publish_time").first()
        try:
            reply_count_i = int(reply_count) if reply_count is not None else None
        except Exception:
            reply_count_i = None
        defaults = {
            'title': (title or '').strip() or None,
            'author': (author or '').strip() or None,
            'description': (description or '').strip() or None,
            'reply_count': reply_count_i if reply_count_i is not None else 0,
            'source': source,
            'category': category,
            'url_hash': url_hash,
            'is_active': True,
        }
        if existing is None:
            defaults['publish_time'] = publish_time or timezone.now()
        else:
            if (source != "nga") and publish_time and not getattr(existing, "publish_time", None):
                defaults['publish_time'] = publish_time
        obj, _ = WowArticle.objects.update_or_create(url=url, defaults=defaults)
        return obj

    def _fetch_nga_main_post(self, url):
        """
        抓取 NGA 主楼内容（用于 Portal 悬浮预览）。
        仅返回纯文本，做适度截断与清洗。
        """
        url = (url or '').strip()
        if not url:
            return ""
        cookies = ''
        try:
            domain = urlparse(url).netloc
            auth = TargetAuth.objects.filter(domain=domain).first()
            if auth and auth.cookie:
                cookies = auth.cookie
        except Exception:
            cookies = ''
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://bbs.nga.cn/'}
        html_text = ''
        try:
            resp = self.req.get(url, 'Response', 0, cookies, headers=headers)
            if resp and getattr(resp, 'status_code', 0) == 200:
                html_text = (resp.text or '')
        except Exception:
            html_text = ''
        if not html_text:
            return ""
        try:
            from bs4 import BeautifulSoup
        except Exception:
            return ""
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            # NGA 主楼一般是 postcontent0 / postcontent1
            el = soup.find(id=re.compile(r'^postcontent0$')) or soup.find(id=re.compile(r'^postcontent1$'))
            if not el:
                el = soup.find(id=re.compile(r'^postcontent\d+$'))
            if not el:
                return ""
            for tag in el(['script', 'style']):
                tag.decompose()
            txt = el.get_text(separator='\n', strip=True)
            txt = re.sub(r'\n{3,}', '\n\n', txt).strip()
            if len(txt) > 1800:
                txt = txt[:1800].rstrip() + '...'
            return txt
        except Exception:
            return ""

    def update_blueposts(self):
        rss_url = 'https://us.forums.blizzard.com/en/wow/g/blizzard-tracker/posts.rss'
        try:
            resp = self.req.get(rss_url, 'Response', 0, '', headers={'User-Agent': 'Mozilla/5.0'})
            if not resp or resp.status_code != 200:
                return
            root = ET.fromstring(resp.text)
            channel = root.find('channel')
            if channel is None:
                return
            i = 0
            for it in channel.findall('item'):
                title = (it.findtext('title') or '').strip()
                link = (it.findtext('link') or '').strip()
                pub_date = (it.findtext('pubDate') or '').strip()
                dt = None
                if pub_date:
                    try:
                        dt = email.utils.parsedate_to_datetime(pub_date)
                        if dt and dt.tzinfo:
                            dt = dt.astimezone(timezone.get_current_timezone())
                    except Exception:
                        dt = None
                creator = ''
                for child in list(it):
                    if child.tag.endswith('creator'):
                        creator = (child.text or '').strip()
                        break
                if not title or not link:
                    continue
                obj = self._upsert_article(
                    title=title,
                    url=link,
                    source='blizzard_tracker',
                    category='bluepost',
                    author=creator,
                    description=None,
                    publish_time=dt,
                )
                # 1) 抓取主楼内容并落库（content/description）
                # 2) 标题/内容分段走 GLM 翻译（title_cn/content_cn）
                if obj and self._translate_budget > 0:
                    did = self._ensure_article_filled_and_translated(obj, source='blizzard_tracker')
                    if did:
                        self._translate_budget -= 1
                i += 1
                if i >= 20:
                    break
        except Exception as e:
            logger.error(f"[PortalPostMonitor] blueposts error: {str(e)}")

    def _ensure_article_filled_and_translated(self, article: WowArticle, source: str) -> bool:
        """
        保证端到端链路可跑通：
        - 先把正文抓到 content（以及 description）
        - 再把 title/content 分段翻译写入 title_cn/content_cn
        - 若失败，下次 scan 继续补齐（不会永久跳过）
        """
        updated = []
        try:
            # 先补正文（抓不到就下次再试）
            need_fetch = (not (article.content or "").strip()) or len((article.content or "").strip()) < 200
            if need_fetch and source == "blizzard_tracker":
                blocks = self._fetch_blizzard_tracker_blocks(article.url)
                body = blocks_to_plain_text(blocks)
                if body:
                    article.content = body
                    if blocks and not (getattr(article, "content_blocks", "") or "").strip():
                        article.content_blocks = dumps_blocks(blocks)
                        updated.append("content_blocks")
                    if not (article.description or "").strip():
                        article.description = body[:1200]
                    updated.extend(["content", "description"])

            if updated:
                article.save(update_fields=list(set(updated)))

            did_translate = self.translation_service.translate_article_fields(
                article,
                logger_prefix="PortalPostMonitor",
            )
            return bool(updated or did_translate)
        except Exception as e:
            logger.error(f"[PortalPostMonitor] fill/translate article error: {e}; article_id={getattr(article, 'id', None)} url={getattr(article, 'url', '')}")
            try:
                if updated:
                    article.save(update_fields=list(set(updated)))
                    return True
            except Exception:
                pass
            return False

    def _fetch_blizzard_tracker_body(self, url: str) -> str:
        return blocks_to_plain_text(self._fetch_blizzard_tracker_blocks(url))

    def _fetch_blizzard_tracker_blocks(self, url: str):
        url = (url or "").strip()
        if not url:
            return []
        headers = {
            # 论坛对不同 UA 的 HTML 结构差异较大，Firefox UA 更稳定
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Referer": "https://us.forums.blizzard.com/",
        }
        html_text = ""
        try:
            resp = self.req.get(url, "Response", 0, "", headers=headers)
            if resp and getattr(resp, "status_code", 0) == 200:
                html_text = resp.text or ""
        except Exception:
            html_text = ""
        if not html_text:
            return []
        blocks = extract_structured_article(html_text, base_url=url, source="blizzard_tracker")
        if blocks:
            return upload_article_images_in_blocks(
                blocks,
                req=self.req,
                article_url=url,
                source="blizzard_tracker",
            )
        try:
            from bs4 import BeautifulSoup
        except Exception:
            return []
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
                tag.decompose()
            # discourse crawler 结构：.topic-body.crawler-post 里会有 div.post=itemprop text
            el = soup.select_one(".topic-body.crawler-post .post") or soup.select_one(".crawler-post .post")
            if not el:
                el = soup.find("div", class_="post")
            if not el:
                return []
            txt = el.get_text(separator="\n", strip=True)
            txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
            return plain_text_to_blocks(txt)
        except Exception:
            return []

    def update_exwind_latest(self):
        try:
            seen = set()
            items = []
            resp = self.req.get('https://exwind.net/', 'Response', 0, '', headers={'User-Agent': 'Mozilla/5.0'})
            if not resp or resp.status_code != 200:
                return
            html_text = resp.text or ''
            for m in re.finditer(r'<a[^>]+href=[\'"]([^\'"]*?/post/[^\'"]+)[\'"][^>]*>(.*?)</a>', html_text, flags=re.I | re.S):
                href = (m.group(1) or '').strip()
                title_raw = (m.group(2) or '').strip()
                title = re.sub(r'<[^>]+>', '', title_raw)
                title = html.unescape(title).strip()
                if not href or '/post/' not in href:
                    continue
                href = urljoin('https://exwind.net/', href)
                if not title or '阅读全文' in title or '返回列表' in title:
                    continue
                if href in seen:
                    continue
                seen.add(href)
                items.append({'title': title, 'url': href})

            enriched = []
            for it in items:
                dt = self._get_exwind_publish_time(it['url'])
                enriched.append((dt or timezone.now(), it))
                if len(enriched) >= 40:
                    break
            enriched.sort(key=lambda x: x[0], reverse=True)
            for dt, it in enriched[:20]:
                desc_full = None
                try:
                    existing = WowArticle.objects.filter(url=it['url']).only("id", "description").first()
                except Exception:
                    existing = None
                if not existing or not (getattr(existing, "description", "") or "").strip() or len((getattr(existing, "description", "") or "")) < 800:
                    desc_full = self._fetch_full_text(it['url'], source='exwind')
                self._upsert_article(
                    title=it['title'],
                    url=it['url'],
                    source='exwind',
                    category='news',
                    author=None,
                    description=desc_full,
                    publish_time=dt,
                )
        except Exception as e:
            logger.error(f"[PortalPostMonitor] exwind error: {str(e)}")

    def update_blizzard_cn_news(self):
        src = "https://wow.blizzard.cn/news/"
        try:
            resp = self.req.get(src, 'Response', 0, '', headers={'User-Agent': 'Mozilla/5.0'})
            if not resp or resp.status_code != 200:
                return
            html_text = (getattr(resp, 'content', b'') or b'').decode('utf-8', 'ignore')
            seen = set()
            added = 0
            for m in re.finditer(
                r'<a[^>]+href="(https?://wow\.blizzard\.cn/news/[^"]+)"[^>]*>([\s\S]*?)</a>',
                html_text,
                flags=re.I
            ):
                if added >= 20:
                    break
                url = (m.group(1) or '').strip()
                block_html = m.group(2) or ''
                if not url or url in seen:
                    continue
                seen.add(url)

                mt = re.search(r'class="list-title"[^>]*>(.*?)</div>', block_html, flags=re.I | re.S)
                title = ''
                if mt:
                    title = re.sub(r'<[^>]+>', '', mt.group(1) or '')
                    title = html.unescape(title)
                    title = re.sub(r'\s+', ' ', title).strip()

                md = re.search(r'class="list-desc"[^>]*>(.*?)</div>', block_html, flags=re.I | re.S)
                desc = ''
                if md:
                    desc = re.sub(r'<[^>]+>', '', md.group(1) or '')
                    desc = html.unescape(desc)
                    desc = re.sub(r'\s+', ' ', desc).strip()

                dt = None
                mday = re.search(r'class="list-time"[^>]*data-time="([0-9]{4}-[0-9]{2}-[0-9]{2})"', block_html, flags=re.I)
                if mday:
                    day = (mday.group(1) or '').strip()
                    if day:
                        try:
                            dt_raw = datetime.datetime.strptime(day, "%Y-%m-%d")
                            dt = timezone.make_aware(dt_raw, timezone.get_current_timezone())
                        except Exception:
                            dt = None

                if not title:
                    continue

                desc_full = None
                try:
                    existing = WowArticle.objects.filter(url=url).only("id", "description").first()
                except Exception:
                    existing = None
                if not existing or not (getattr(existing, "description", "") or "").strip() or len((getattr(existing, "description", "") or "")) < 800:
                    desc_full = self._fetch_full_text(url, source='blizzard_cn')

                self._upsert_article(
                    title=title,
                    url=url,
                    source='blizzard_cn',
                    category='news',
                    author=None,
                    description=desc_full or (desc or None),
                    publish_time=dt or timezone.now(),
                )
                added += 1
        except Exception as e:
            logger.error(f"[PortalPostMonitor] blizzard_cn_news error: {str(e)}")

    def _fetch_full_text(self, url, source=''):
        try:
            resp = self.req.get(url, 'Response', 0, '', headers={'User-Agent': 'Mozilla/5.0'})
            if not resp or resp.status_code != 200:
                return None
            html_text = resp.text or ''
            if not html_text:
                return None
            if source == 'blizzard_cn':
                return self._extract_blizzard_cn_body(html_text)
            if source == 'exwind':
                return self._extract_exwind_body(html_text)
            return self._strip_html_text(html_text)
        except Exception:
            return None

    def _strip_html_text(self, raw_html):
        t = raw_html or ""
        t = re.sub(r'<(script|style|noscript)[^>]*>[\s\S]*?</\1>', '', t, flags=re.I)
        t = re.sub(r'(?i)<br\s*/?>', '\n', t)
        t = re.sub(r'(?i)</p\s*>', '\n\n', t)
        t = re.sub(r'(?i)</div\s*>', '\n\n', t)
        t = re.sub(r'(?i)</li\s*>', '\n', t)
        t = re.sub(r'<[^>]+>', '', t)
        t = html.unescape(t)
        t = t.replace('\r\n', '\n').replace('\r', '\n')
        lines = [ln.strip() for ln in t.split('\n')]
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
        return text or None

    def _extract_blizzard_cn_body(self, html_text):
        t = html_text or ""
        blocks = []
        for pat in (
            r'<div[^>]+class="[^"]*(?:detail-desc|detail-content|news-detail)[^"]*"[^>]*>([\s\S]*?)</div>',
            r'<article[^>]*>([\s\S]*?)</article>',
        ):
            m = re.search(pat, t, flags=re.I)
            if m:
                blocks.append(m.group(1) or "")
        if not blocks:
            return None
        raw = max(blocks, key=lambda x: len(x or ""))
        return self._strip_html_text(raw)

    def _extract_exwind_body(self, html_text):
        t = html_text or ""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(t, "html.parser")
            el = soup.select_one(".EXWIND_content") or soup.select_one("#EXWIND_capture_area .EXWIND_content")
            if el:
                for tag in el(["script", "style", "noscript", "button", "svg"]):
                    tag.decompose()
                text = el.get_text(separator="\n", strip=True)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if text:
                    return text
        except Exception:
            pass

        blocks = []
        for pat in (
            r'<div[^>]+class="[^"]*(?:EXWIND_content|entry-content|post-content|content)[^"]*"[^>]*>([\s\S]*?)</div>',
            r'<article[^>]*>([\s\S]*?)</article>',
        ):
            m = re.search(pat, t, flags=re.I)
            if m:
                blocks.append(m.group(1) or "")
        if not blocks:
            return None
        raw = max(blocks, key=lambda x: len(x or ""))
        return self._strip_html_text(raw)

    def _get_exwind_publish_time(self, url):
        try:
            resp = self.req.get(url, 'Response', 0, '', headers={'User-Agent': 'Mozilla/5.0'})
            if not resp or resp.status_code != 200:
                return None
            t = resp.text or ''
            m = re.search(r'网站发布\s*[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s+([0-9]{2}:[0-9]{2}:[0-9]{2})', t)
            if m:
                dt = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
                return timezone.make_aware(dt, timezone.get_current_timezone())

            site_time = None
            m = re.search(r'网站发布\s*[:：]?\s*([0-9]{2}:[0-9]{2}:[0-9]{2})', t)
            if m:
                site_time = m.group(1)

            m = re.search(r'PUBLISHED\s+AT\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s+([0-9]{2}:[0-9]{2})(?::([0-9]{2}))?', t, flags=re.I)
            if m:
                s = f"{m.group(1)} {m.group(2)}:{m.group(3) or '00'}"
                dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                return timezone.make_aware(dt, timezone.get_current_timezone())

            m = re.search(r'蓝贴发布\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s+([0-9]{2}:[0-9]{2}:[0-9]{2})', t)
            if m:
                day = m.group(1)
                tm = m.group(2)
                if site_time:
                    tm = site_time
                dt = datetime.datetime.strptime(f"{day} {tm}", "%Y-%m-%d %H:%M:%S")
                return timezone.make_aware(dt, timezone.get_current_timezone())

            m = re.search(r'([0-9]{4}-[0-9]{2}-[0-9]{2})\s+([0-9]{2}:[0-9]{2})(?::([0-9]{2}))?', t)
            if m:
                s = f"{m.group(1)} {m.group(2)}:{m.group(3) or '00'}"
                dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                return timezone.make_aware(dt, timezone.get_current_timezone())

            return None
        except Exception:
            return None

    def update_nga_hot(self):
        try:
            if self.req and getattr(self.req, 'is_chrome', False):
                driver = self.req.get('https://bbs.nga.cn/thread.php?fid=7', 'RespByChrome', 0, '', is_origin=1)
                if not driver:
                    return
                time.sleep(3)
                try:
                    driver.run_js("g()")
                except Exception:
                    pass
                try:
                    rows = driver.ele('#topicrows').eles('tag:tbody') or []
                except Exception:
                    rows = []
                seen = set()
                black_list = ["公益", "代工", "支持跨服"]
                added = 0
                for row in rows:
                    try:
                        tds = row.eles('tag:td') or []
                    except Exception:
                        continue
                    if len(tds) < 3:
                        continue
                    try:
                        title_ele = tds[1].ele('.:topic')
                        post_link = self._normalize_nga_url(title_ele.link)
                        post_name = title_ele.text
                    except Exception:
                        continue
                    reply_count = 0
                    try:
                        reply_count = int((tds[0].text or '0').strip() or '0')
                    except Exception:
                        reply_count = 0
                    post_link = (post_link or '').strip()
                    post_name = (post_name or '').strip()
                    if not post_link or not post_name:
                        continue
                    bad = False
                    for b in black_list:
                        if b in post_name:
                            bad = True
                            break
                    if bad:
                        continue
                    if post_link in seen:
                        continue
                    seen.add(post_link)
                    obj = self._upsert_article(
                        title=post_name,
                        url=post_link,
                        source='nga',
                        category='hot',
                        author=None,
                        description=None,
                        publish_time=None,
                        reply_count=reply_count,
                    )
                    # 补抓主楼内容（用于 Portal 悬浮预览）
                    try:
                        if obj and (not (getattr(obj, 'content', '') or '').strip() or len((getattr(obj, 'content', '') or '')) < 80):
                            content = self._fetch_nga_main_post(post_link)
                            if content:
                                obj.content = content
                                obj.save(update_fields=['content'])
                    except Exception:
                        pass
                    added += 1
                    if added >= 30:
                        break
                return

            seen = set()
            black_list = ["公益", "代工", "支持跨服"]
            added = 0

            urls = ['https://bbs.nga.cn/thread.php?fid=7']
            html_text = ''
            had_forbidden = False
            last_domain = 'bbs.nga.cn'
            for u in urls:
                cookies = ''
                try:
                    domain = urlparse(u).netloc
                    last_domain = domain or last_domain
                    auth = TargetAuth.objects.filter(domain=domain).first()
                    if auth and auth.cookie:
                        cookies = auth.cookie
                except Exception:
                    cookies = ''
                headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://bbs.nga.cn/'}
                resp = self.req.get(u, 'Response', 0, cookies, headers=headers)
                if resp and resp.status_code == 200 and (resp.text or '').strip():
                    html_text = resp.text
                    break
                if resp and resp.status_code == 403:
                    had_forbidden = True
                    continue
            if not html_text:
                if had_forbidden:
                    upsert_system_alert(
                        category='NGA_COOKIE_REQUIRED',
                        subject=last_domain,
                        level=3,
                        title='NGA Cookie 失效或缺失',
                        content=f"NGA 请求返回 403，可能 cookie 缺失或过期，请更新 TargetAuth(domain={last_domain}) 的 cookie。"
                    )
                return

            for m in re.finditer(r'<a[^>]+href=[\'"]([^\'"]*read\.php\?tid=\d+[^\'"]*)[\'"][^>]*>(.*?)</a>', html_text, flags=re.I | re.S):
                href = (m.group(1) or '').strip()
                title_raw = (m.group(2) or '').strip()
                title = re.sub(r'<[^>]+>', '', title_raw)
                title = html.unescape(title).strip()
                if not href or not title:
                    continue
                post_link = self._normalize_nga_url(href)
                post_name = title
                bad = False
                for b in black_list:
                    if b in post_name:
                        bad = True
                        break
                if bad:
                    continue
                if post_link in seen:
                    continue
                seen.add(post_link)
                obj = self._upsert_article(
                    title=post_name,
                    url=post_link,
                    source='nga',
                    category='hot',
                    author=None,
                    description=None,
                    publish_time=None,
                )
                # 补抓主楼内容（用于 Portal 悬浮预览）
                try:
                    if obj and (not (getattr(obj, 'content', '') or '').strip() or len((getattr(obj, 'content', '') or '')) < 80):
                        content = self._fetch_nga_main_post(post_link)
                        if content:
                            obj.content = content
                            obj.save(update_fields=['content'])
                except Exception:
                    pass
                added += 1
                if added >= 30:
                    break
        except Exception as e:
            logger.error(f"[PortalPostMonitor] nga error: {str(e)}")
