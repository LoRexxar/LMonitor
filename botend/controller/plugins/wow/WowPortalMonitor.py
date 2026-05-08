import json
import time
import xml.etree.ElementTree as ET

import requests

from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.models import PortalCache


class WowPortalMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    @staticmethod
    def _set_cache(key, data, status=0, error_message=''):
        try:
            raw = json.dumps(data, ensure_ascii=False)
        except Exception:
            raw = ''
        PortalCache.objects.update_or_create(
            key=key,
            defaults={
                'data': raw,
                'status': status,
                'error_message': error_message or '',
            }
        )

    def scan(self, url):
        self.update_exwind_latest()
        self.update_nga_hot()
        self.update_blueposts()
        return True

    def update_nga_hot(self):
        key = 'nga_hot'
        try:
            driver = self.req.get('https://nga.178.com/thread.php?fid=7', 'RespByChrome', 0, '', is_origin=1)
            if not driver:
                self._set_cache(key, [], status=1, error_message='NGA 获取失败')
                return

            time.sleep(3)
            try:
                driver.run_js("g()")
            except Exception:
                pass

            rows = []
            try:
                rows = driver.ele('#topicrows').eles('tag:tbody') or []
            except Exception:
                rows = []

            items = []
            seen = set()
            black_list = ["公益", "代工", "支持跨服"]
            for row in rows:
                try:
                    tds = row.eles('tag:td') or []
                except Exception:
                    continue
                if len(tds) < 3:
                    continue

                try:
                    reply_count = int((tds[0].text or '0').strip() or '0')
                except Exception:
                    reply_count = 0

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

                try:
                    post_time = tds[2].text.strip()
                except Exception:
                    post_time = ''

                items.append({
                    'title': post_name,
                    'url': post_link,
                    'source': 'NGA',
                    'source_url': post_link,
                    'time': post_time,
                    'reply_count': reply_count,
                })

            items.sort(key=lambda x: x.get('reply_count', 0), reverse=True)
            top_items = items[:12]
            if not top_items:
                existing = PortalCache.objects.filter(key=key).first()
                if existing and (existing.data or '').strip() not in ['', '[]']:
                    existing.status = 1
                    existing.error_message = 'NGA 解析为空'
                    existing.save(update_fields=['status', 'error_message', 'updated_at'])
                    return
                self._set_cache(key, [], status=1, error_message='NGA 解析为空')
                return

            self._set_cache(key, top_items, status=0, error_message='')
        except Exception as e:
            logger.error(f"[WowPortalMonitor] NGA update error: {str(e)}")
            existing = PortalCache.objects.filter(key=key).first()
            if existing and (existing.data or '').strip() not in ['', '[]']:
                existing.status = 1
                existing.error_message = 'NGA 解析失败'
                existing.save(update_fields=['status', 'error_message', 'updated_at'])
                return
            self._set_cache(key, [], status=1, error_message='NGA 解析失败')

    def update_blueposts(self):
        key = 'blueposts'
        try:
            rss_url = 'https://us.forums.blizzard.com/en/wow/g/blizzard-tracker/posts.rss'
            resp = requests.get(rss_url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                self._set_cache(key, [], status=1, error_message=f'蓝帖RSS失败({resp.status_code})')
                return

            root = ET.fromstring(resp.text)
            channel = root.find('channel')
            if channel is None:
                self._set_cache(key, [], status=1, error_message='蓝帖RSS解析失败')
                return

            items = []
            for it in channel.findall('item'):
                title = (it.findtext('title') or '').strip()
                link = (it.findtext('link') or '').strip()
                pub_date = (it.findtext('pubDate') or '').strip()
                creator = ''
                for child in list(it):
                    if child.tag.endswith('creator'):
                        creator = (child.text or '').strip()
                        break
                if not title or not link:
                    continue
                items.append({
                    'title': title,
                    'url': link,
                    'source': 'Blizz Tracker',
                    'source_url': link,
                    'time': pub_date,
                    'author': creator,
                })
                if len(items) >= 12:
                    break

            self._set_cache(key, items, status=0, error_message='')
        except Exception as e:
            logger.error(f"[WowPortalMonitor] blueposts update error: {str(e)}")
            self._set_cache(key, [], status=1, error_message='蓝帖获取失败')

    def update_exwind_latest(self):
        key = 'exwind_latest'
        try:
            driver = self.req.get('https://exwind.net/', 'RespByChrome', 0, '', is_origin=1)
            if not driver:
                self._set_cache(key, [], status=1, error_message='EXWIND 获取失败')
                return

            time.sleep(2)
            anchors = driver.eles('tag:a') or []
            items = []
            seen = set()
            for a in anchors:
                try:
                    href = getattr(a, 'link', None) or a.attr('href') or ''
                except Exception:
                    href = ''
                href = (href or '').strip()
                if not href:
                    continue
                if '/post/' not in href:
                    continue
                try:
                    title = (a.text or '').strip()
                except Exception:
                    title = ''
                if not title:
                    continue
                if '阅读全文' in title or '返回列表' in title:
                    continue
                if href in seen:
                    continue
                seen.add(href)
                items.append({
                    'title': title,
                    'url': href,
                    'source': 'EXWIND',
                    'source_url': href,
                })
                if len(items) >= 12:
                    break

            self._set_cache(key, items, status=0, error_message='')
        except Exception as e:
            logger.error(f"[WowPortalMonitor] EXWIND update error: {str(e)}")
            self._set_cache(key, [], status=1, error_message='EXWIND 解析失败')
