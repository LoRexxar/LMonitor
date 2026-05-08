import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
import email.utils
import datetime

from botend.controller.BaseScan import BaseScan
from botend.models import TargetAuth, WowArticle
from botend.alerting import upsert_system_alert
from django.utils import timezone
from utils.log import logger
from urllib.parse import urljoin, urlparse


def _hash_url(url):
    u = (url or '').strip()
    if not u:
        return None
    return hashlib.sha256(u.encode('utf-8')).hexdigest()


class PortalPostMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        self.update_blueposts()
        self.update_exwind_latest()
        self.update_nga_hot()
        return True

    def _upsert_article(self, *, title, url, source, category, author=None, description=None, publish_time=None):
        url = (url or '').strip()
        if not url:
            return
        url_hash = _hash_url(url)
        defaults = {
            'title': (title or '').strip() or None,
            'author': (author or '').strip() or None,
            'description': (description or '').strip() or None,
            'publish_time': publish_time or timezone.now(),
            'source': source,
            'category': category,
            'url_hash': url_hash,
            'is_active': True,
        }
        WowArticle.objects.update_or_create(url=url, defaults=defaults)

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
                self._upsert_article(
                    title=title,
                    url=link,
                    source='blizzard_tracker',
                    category='bluepost',
                    author=creator,
                    description=None,
                    publish_time=dt,
                )
                i += 1
                if i >= 20:
                    break
        except Exception as e:
            logger.error(f"[PortalPostMonitor] blueposts error: {str(e)}")

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
                self._upsert_article(
                    title=it['title'],
                    url=it['url'],
                    source='exwind',
                    category='news',
                    author=None,
                    description=None,
                    publish_time=dt,
                )
        except Exception as e:
            logger.error(f"[PortalPostMonitor] exwind error: {str(e)}")

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
                driver = self.req.get('https://nga.178.com/thread.php?fid=7', 'RespByChrome', 0, '', is_origin=1)
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
                        post_link = title_ele.link
                        post_name = title_ele.text
                    except Exception:
                        continue
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
                    self._upsert_article(
                        title=post_name,
                        url=post_link,
                        source='nga',
                        category='hot',
                        author=None,
                        description=None,
                        publish_time=None,
                    )
                    added += 1
                    if added >= 30:
                        break
                return

            seen = set()
            black_list = ["公益", "代工", "支持跨服"]
            added = 0

            urls = [
                'https://bbs.nga.cn/thread.php?fid=7',
                'https://nga.178.com/thread.php?fid=7',
            ]
            html_text = ''
            had_forbidden = False
            last_domain = 'nga.178.com'
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
                headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://nga.178.com/'}
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
                post_link = urljoin('https://nga.178.com/', href)
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
                self._upsert_article(
                    title=post_name,
                    url=post_link,
                    source='nga',
                    category='hot',
                    author=None,
                    description=None,
                    publish_time=None,
                )
                added += 1
                if added >= 30:
                    break
        except Exception as e:
            logger.error(f"[PortalPostMonitor] nga error: {str(e)}")
