"""Icy Veins 魔兽新闻采集：限定新闻路径，复用门户的入库及翻译链路。"""
import json
import re
import hashlib
import html
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import WowArticle, MonitorTaskLeaseLost
from botend.services.article_content_service import blocks_to_plain_text, dumps_blocks, extract_structured_article
from utils.log import logger


HOME_URL = 'https://www.icy-veins.com/wow/'
RSS_URL = 'https://wp-prod.icy-veins.com/custom-rss/?category=wow'
SOURCE = 'icy_veins'
BROWSER_READY_TIMEOUT = 45
NEWS_LABELS = {'wow': '正式服', 'wow classic': '怀旧服', 'wow forever': '无限'}
NEWS_PREFIX = re.compile(r'^\[(?:正式服|怀旧服|无限|魔兽世界)(?:/(?:正式服|怀旧服|无限))*\]\s*')


def label_title(title, categories=()):
    """只使用上游明确分类；没有分类时不猜测具体版本。"""
    categories = {part.strip().casefold() for value in categories for part in value.split(',')}
    labels = [label for key, label in NEWS_LABELS.items() if key in categories]
    prefix = '[' + '/'.join(labels or ['魔兽世界']) + ']'
    return (prefix + NEWS_PREFIX.sub('', title or '').strip())[:255]


def sync_title_label(article, source_title):
    """给已采集新闻补齐分类标签，保留原文和已有译文。"""
    match = NEWS_PREFIX.match(source_title)
    if not match:
        return
    prefix = match.group().strip()
    if prefix == '[魔兽世界]' and NEWS_PREFIX.match(article.title or ''):
        return
    fields = []
    for field in ('title', 'title_cn'):
        current = getattr(article, field, '') or ''
        if current:
            updated = (prefix + NEWS_PREFIX.sub('', current).strip())[:255]
            if updated != current:
                setattr(article, field, updated)
                fields.append(field)
    if fields:
        article.save(update_fields=fields)


def parse_rss(text):
    """兼容原始 XML 和 Chromium XML 查看器中的原始 rss 节点。"""
    text = text or ''
    if len(text) > 8 * 1024 * 1024 or re.search(r'<!DOCTYPE|<!ENTITY', text, re.I):
        return []
    match = re.search(r'<rss\b[\s\S]*?</rss\s*>', text)
    if not match:
        return []
    try:
        root = ET.fromstring(match.group())
    except ET.ParseError:
        return []
    posts, seen = [], set()
    for item in root.findall('./channel/item')[:100]:
        url = news_url(item.findtext('link'))
        title = html.unescape(item.findtext('title') or '').strip()
        if not url or not title or url in seen:
            continue
        try:
            published = parsedate_to_datetime(item.findtext('pubDate') or '')
        except (ValueError, TypeError, OverflowError):
            continue
        if timezone.is_naive(published):
            continue
        seen.add(url)
        posts.append({'url': url, 'title': label_title(title, [node.text or '' for node in item.findall('category')]), 'publish_time': published,
                      'author': (item.findtext('{http://purl.org/dc/elements/1.1/}creator') or '')[:255],
                      'description': item.findtext('description') or '',
                      'content_html': item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or ''})
    return posts


def fetch_rss(req):
    return parse_rss(fetch_html(req, RSS_URL, parse_rss))


def news_url(value):
    """统一尾斜杠和追踪参数，只接受本站 WoW 新闻详情。"""
    parsed = urlsplit(urljoin(HOME_URL, str(value or '').strip()))
    if (parsed.scheme not in ('http', 'https')
            or parsed.netloc.lower() not in ('www.icy-veins.com', 'icy-veins.com')
            or not re.fullmatch(r'/wow/news/[^/]+/?', parsed.path)):
        return ''
    return 'https://www.icy-veins.com' + parsed.path.rstrip('/') + '/'


def parse_listing(html_text):
    soup = BeautifulSoup(html_text or '', 'html.parser')
    posts, seen = [], set()
    for anchor in soup.select('a[href]'):
        url = news_url(anchor.get('href'))
        if not url or url in seen:
            continue
        heading = anchor.select_one('h2, h3, h4, .nt-card__title, .title, .news-title')
        title = (heading.get_text(' ', strip=True) if heading else
                 anchor.get('title') or anchor.get('aria-label') or anchor.get_text(' ', strip=True))
        title = re.sub(r'\s+\d+\s*[mhd]\s+ago\s+by\s+.*$', '', title, flags=re.I).strip()
        if not title:
            continue
        seen.add(url)
        posts.append({'url': url, 'title': label_title(title)})
    return posts[:20]


def _article_json(soup):
    def walk(value):
        if isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, dict):
            kind = value.get('@type', [])
            if isinstance(kind, str):
                kind = [kind]
            if any(item in ('Article', 'NewsArticle', 'BlogPosting') for item in kind):
                yield value
            yield from walk(value.get('@graph', []))
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue


def parse_detail(html_text, url):
    soup = BeautifulSoup(html_text or '', 'html.parser')
    metadata = next(_article_json(soup), {})
    def meta(selector):
        node = soup.select_one(selector)
        return node.get('content', '') if node else ''
    heading = soup.select_one('h1')
    title = heading.get_text(' ', strip=True) if heading else metadata.get('headline', '')
    date_text = meta('meta[property="article:published_time"]') or metadata.get('datePublished')
    if not date_text:
        node = soup.select_one('time[datetime]')
        date_text = node.get('datetime') if node else ''
    try:
        published = parse_datetime(str(date_text or ''))
    except ValueError:
        published = None
    # 不用采集时间冒充发布日期；无时区的上游日期也不自行猜测。
    if published and timezone.is_naive(published):
        published = None
    author = metadata.get('author', {})
    if isinstance(author, list):
        author = author[0] if author else {}
    author = author.get('name', '') if isinstance(author, dict) else str(author or '')
    # 当前页面的结构化数据把作者写成站点名，以实际署名优先。
    byline = soup.select_one('.news-author a')
    if byline and byline.get_text(strip=True):
        author = byline.get_text(' ', strip=True)
    blocks = extract_structured_article(html_text, base_url=url, source=SOURCE)
    categories = metadata.get('articleSection') or []
    if isinstance(categories, str):
        categories = [categories]
    categories = [str(value) for value in categories] + [meta('meta[property="article:section"]')]
    return {'title': label_title(str(title), categories) if title else '', 'author': (meta('meta[name="author"]') or author)[:255],
            'description': meta('meta[property="og:description"]') or meta('meta[name="description"]'),
            'publish_time': published, 'blocks': blocks}


def fetch_html(req, url, valid):
    """普通请求优先，已启用浏览器时回退；验证页和不完整页面不算成功。"""
    try:
        response = req.get(url, 'Response', 0, '')
        if response is not None and response is not False and response.status_code == 200:
            text = response.text or ''
            if valid(text):
                return text
    except MonitorTaskLeaseLost:
        raise
    except Exception as exc:
        logger.warning('[IcyVeins] 请求失败：{}'.format(type(exc).__name__))
    if getattr(req, 'is_chrome', False):
        driver = req.get(url, 'RespByChrome', 0, '', is_origin=1)
        if driver:
            deadline = time.monotonic() + BROWSER_READY_TIMEOUT
            while True:
                text = getattr(driver, 'html', '') or ''
                if valid(text):
                    return text
                if time.monotonic() >= deadline:
                    break
                # 等待浏览器自行完成导航与脚本加载，不点击验证码，不重复导航。
                time.sleep(1)
    raise ValueError('Icy Veins 未返回有效新闻页面（可能被站点验证拦截）')


def update_news(monitor):
    """重复采集复用已存正文；单篇故障不阻断其他文章，翻译另有补偿任务。"""
    try:
        posts = fetch_rss(monitor.req)
    except MonitorTaskLeaseLost:
        raise
    except Exception as exc:
        logger.warning('[IcyVeins] 分类 RSS 不可用，尝试 WoW 首页：{}'.format(type(exc).__name__))
        posts = []
    if not posts:
        posts = parse_listing(fetch_html(monitor.req, HOME_URL, parse_listing))
    failures, budget = [], 3
    for item in posts:
        try:
            article = WowArticle.objects.filter(url_hash=hashlib.sha256(item['url'].encode('utf-8')).hexdigest(), source=SOURCE).first()
            if not article or not article.content or not article.publish_time:
                blocks = extract_structured_article(
                    '<article class="article-content">' + item.get('content_html', '') + '</article>',
                    base_url=item['url'], source=SOURCE,
                ) if item.get('content_html') else []
                if blocks_to_plain_text(blocks).strip() and item.get('publish_time'):
                    detail = {key: item[key] for key in ('title', 'author', 'description', 'publish_time')}
                else:
                    def valid(text):
                        detail = parse_detail(text, item['url'])
                        return detail['title'] and detail['publish_time'] and blocks_to_plain_text(detail['blocks']).strip()
                    detail = parse_detail(fetch_html(monitor.req, item['url'], valid), item['url'])
                    blocks = detail.pop('blocks')
                article = monitor._upsert_article(url=item['url'], source=SOURCE, category='news', **detail)
                article.content = blocks_to_plain_text(blocks)
                article.content_blocks = dumps_blocks(blocks)
                article.save(update_fields=['content', 'content_blocks'])
            sync_title_label(article, item['title'])
            if budget and monitor.translation_service.available() and monitor.translation_service.needs_translation(article):
                budget -= 1
                monitor.translation_service.translate_article_fields(article, logger_prefix='IcyVeins')
        except MonitorTaskLeaseLost:
            raise
        except Exception as exc:
            failures.append(item['url'])
            logger.warning('[IcyVeins] 文章采集或翻译失败 {}：{}'.format(item['url'], type(exc).__name__))
    if failures:
        raise ValueError('Icy Veins 本轮有 {} 篇新闻处理失败，后续轮次重试'.format(len(failures)))
