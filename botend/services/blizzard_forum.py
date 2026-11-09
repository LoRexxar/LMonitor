"""Blizzard forum board facts. A channel is not a product-applicability claim."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone

from botend.models import (BlizzardForumCategory, BlizzardForumTopic, WowArticle,
                           MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost)

FORUM_URL = 'https://us.forums.blizzard.com/en/wow'
METADATA_TTL_SECONDS = 6 * 60 * 60
METADATA_RETRY_SECONDS = 15 * 60


class TopicIdentityMismatch(ValueError):
    pass


def _positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('invalid_positive_id')
    return value


def _source_path(url):
    try:
        if (not isinstance(url, str) or len(url) > 2000 or '\\' in url
                or any(char.isspace() or ord(char) < 32 for char in url)):
            raise ValueError('invalid_forum_url')
        p = urlsplit(url)
        if (p.scheme != 'https' or p.netloc != 'us.forums.blizzard.com'
                or p.query or p.fragment or not p.path.startswith('/en/wow/')):
            raise ValueError('invalid_forum_url')
        return p.path
    except (TypeError, AttributeError):
        raise ValueError('invalid_forum_url') from None


def topic_id_from_url(url):
    path = _source_path(url)
    # A trailing post number is never the topic identity. No coercion/normalisation.
    short = re.fullmatch(r'/en/wow/t/([1-9][0-9]*)(?:/[1-9][0-9]*)?/?', path)
    if short:
        return int(short[1])
    match = re.fullmatch(r'/en/wow/t/([^/]+)/([1-9][0-9]*)(?:/[1-9][0-9]*)?/?', path)
    if not match or match[1].isdigit():
        raise ValueError('invalid_topic_url')
    return int(match[2])


def channel_payload(game_version, category_id=None, name='', url=''):
    labels = BlizzardForumCategory.GameVersion
    version = str(game_version or '')
    if version not in labels.values:
        version = ''
    return {
        'game_version': version,
        'game_version_label': labels(version).label if version else '',
        'forum_category': {'id': category_id, 'name': name, 'url': url} if category_id else None,
        'game_version_basis': 'forum_category' if category_id else '',
    }


def article_channel(article):
    if article.source != 'blizzard_tracker' or not article.blizzard_topic_id:
        return channel_payload('')
    topic = article.blizzard_topic
    category = topic.category
    if not category:
        return channel_payload('')
    return channel_payload(category.game_version, category.external_id, category.name, category.url)


def _version(chain):
    ids = [c['external_id'] for c in chain]
    # Product channel wins for the explicitly classified Forever Beta board.
    if 349 in ids:
        return BlizzardForumCategory.GameVersion.FOREVER
    if 23 in ids:
        return BlizzardForumCategory.GameVersion.TEST
    if 346 in ids:
        return BlizzardForumCategory.GameVersion.FOREVER
    # Explicit site convention for this board only, not "all non-Classic is retail".
    if ids == [170, 171]:
        return BlizzardForumCategory.GameVersion.RETAIL
    return ''


def parse_topic_metadata(html_text, url):
    expected_id = topic_id_from_url(url)
    soup = BeautifulSoup(html_text, 'html.parser')
    scripts = soup.select('script#data-preloaded')
    if len(scripts) != 1:
        raise ValueError('missing_or_duplicate_preloaded')
    try:
        preloaded = json.loads(scripts[0].string or '')
        if not isinstance(preloaded, dict):
            raise ValueError('invalid_preloaded')
        key = f'topic_{expected_id}'
        if key not in preloaded:
            if any(k.startswith('topic_') for k in preloaded):
                raise TopicIdentityMismatch('response_topic_mismatch')
            raise ValueError('missing_topic')
        topic = json.loads(preloaded[key])
        site = json.loads(preloaded['site'])
        if _positive_id(topic['id']) != expected_id:
            raise TopicIdentityMismatch('response_topic_mismatch')
        category_id = _positive_id(topic['category_id'])
        categories = site['categories']
        if not isinstance(categories, list):
            raise ValueError('invalid_categories')
        by_id = {}
        for category in categories:
            cid = _positive_id(category['id'])
            if cid in by_id:
                raise ValueError('duplicate_category')
            by_id[cid] = category
        chain, seen = [], set()
        cid = category_id
        while cid is not None:
            if cid in seen or cid not in by_id:
                raise ValueError('invalid_category_ancestry')
            seen.add(cid)
            c = by_id[cid]
            name, slug = c['name'], c['slug']
            if not isinstance(name, str) or not name or len(name) > 255:
                raise ValueError('invalid_category_name')
            if not isinstance(slug, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', slug) or len(slug) > 255:
                raise ValueError('invalid_category_slug')
            parent = c.get('parent_category_id')
            if parent is not None:
                _positive_id(parent)
            chain.insert(0, {'external_id': cid, 'name': name, 'slug': slug})
            cid = parent
        if len(chain) > 20:
            raise ValueError('category_depth_exceeded')
        breadcrumbs = soup.select('.topic-category[itemtype$="/BreadcrumbList"]')
        if len(breadcrumbs) != 1:
            raise ValueError('missing_or_duplicate_breadcrumb')
        items = breadcrumbs[0].select('[itemprop="itemListElement"]')
        if len(items) != len(chain):
            raise ValueError('breadcrumb_chain_mismatch')
        path = FORUM_URL + '/c'
        for pos, (item, category) in enumerate(zip(items, chain), 1):
            path += '/' + category['slug']
            category_url = path + '/' + str(category['external_id'])
            anchors = item.select('a[itemprop="item"]')
            names = item.select('[itemprop="name"]')
            positions = item.select('[itemprop="position"]')
            if len(anchors) != 1 or len(names) != 1 or len(positions) != 1:
                raise ValueError('invalid_breadcrumb')
            actual_url = urljoin(FORUM_URL + '/', anchors[0].get('href', ''))
            _source_path(actual_url)
            if (actual_url != category_url or names[0].get_text(strip=True) != category['name']
                    or positions[0].get('content') != str(pos)):
                raise ValueError('breadcrumb_category_mismatch')
            category['url'] = actual_url
            category['game_version'] = _version(chain[:pos])
        return {'forum_url': FORUM_URL, 'topic_id': expected_id, 'categories': chain,
                'source_url': url, 'response_sha256': hashlib.sha256(html_text.encode('utf-8')).hexdigest()}
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid_preloaded_metadata') from exc


def _article_identity(article, topic_id):
    if article.source != 'blizzard_tracker' or topic_id_from_url(article.url) != topic_id:
        raise TopicIdentityMismatch('article_topic_mismatch')


def validate_metadata_lease(task_id, owner):
    """Inside atomic(): scheduler-compatible all-parent → exact-lease fencing."""
    from botend.plugin_sync import monitor_scope_key
    tasks = list(MonitorTask.objects.select_for_update().order_by('id'))
    task = next((row for row in tasks if row.pk == task_id and row.name == 'PortalPostMonitor'), None)
    if task is None:
        raise MonitorTaskLeaseLost('metadata task is missing')
    now = timezone.now()
    scope_ids = [row.pk for row in tasks if monitor_scope_key(row) == monitor_scope_key(task)]
    leases = list(MonitorTaskLease.objects.select_for_update().filter(task_id__in=scope_ids).order_by('task_id'))
    lease = next((row for row in leases if row.task_id == task_id), None)
    if (not isinstance(owner, str) or not owner or lease is None or lease.owner != owner
            or lease.expires_at <= now
            or any(row.task_id != task_id and row.expires_at > now for row in leases)):
        raise MonitorTaskLeaseLost('metadata lease is missing, expired, replaced, or scope-conflicted')


def attach_topic(article, topic, write_guard=None):
    """Attach an existing topic without copying its facts or touching article content."""
    with transaction.atomic():
        if write_guard:
            write_guard()
        locked = WowArticle.objects.select_for_update().get(pk=article.pk)
        _article_identity(locked, topic.external_id)
        if topic.forum_url != FORUM_URL:
            raise TopicIdentityMismatch('topic_forum_mismatch')
        if locked.blizzard_topic_id != topic.pk:
            locked.blizzard_topic = topic
            locked.save(update_fields=['blizzard_topic'])
        article.blizzard_topic = topic
    return topic


def persist_topic_metadata(article, html_text, url, observed_at=None, write_guard=None):
    metadata = parse_topic_metadata(html_text, url)
    _article_identity(article, metadata['topic_id'])
    observed_at = observed_at or timezone.now()
    if timezone.is_naive(observed_at):
        raise ValueError('naive_observation_time')
    with transaction.atomic():
        if write_guard:
            write_guard()
        locked_article = WowArticle.objects.select_for_update().get(pk=article.pk)
        _article_identity(locked_article, metadata['topic_id'])
        topic, _ = BlizzardForumTopic.objects.get_or_create(
            forum_url=FORUM_URL, external_id=metadata['topic_id'])
        topic = BlizzardForumTopic.objects.select_for_update().get(pk=topic.pk)
        if topic.last_observed_at is None or topic.last_observed_at < observed_at:
            parent = None
            for facts in metadata['categories']:
                category, _ = BlizzardForumCategory.objects.get_or_create(
                    forum_url=FORUM_URL, external_id=facts['external_id'],
                    defaults={k: facts[k] for k in ('name', 'slug', 'url', 'game_version')})
                category = BlizzardForumCategory.objects.select_for_update().get(pk=category.pk)
                if category.last_observed_at is None or category.last_observed_at < observed_at:
                    updates = []
                    for field in ('name', 'slug', 'url', 'game_version'):
                        if getattr(category, field) != facts[field]:
                            setattr(category, field, facts[field])
                            updates.append(field)
                    if category.parent_id != (parent.pk if parent else None):
                        category.parent = parent
                        updates.append('parent')
                    category.last_observed_at = observed_at
                    category.save(update_fields=updates + ['last_observed_at'])
                parent = category
            topic.category = parent
            topic.source_url = url
            topic.response_sha256 = metadata['response_sha256']
            topic.last_observed_at = observed_at
            topic.metadata_retry_at = None
            topic.save(update_fields=['category', 'source_url', 'response_sha256', 'last_observed_at', 'metadata_retry_at'])
        if locked_article.blizzard_topic_id != topic.pk:
            locked_article.blizzard_topic = topic
            locked_article.save(update_fields=['blizzard_topic'])
        article.blizzard_topic = topic
    return topic


def record_metadata_failure(topic_id, attempted_at, write_guard=None):
    """Shared bounded cooldown; don't slide an existing future deadline on each read."""
    from datetime import timedelta
    with transaction.atomic():
        if write_guard:
            write_guard()
        topic, _ = BlizzardForumTopic.objects.get_or_create(forum_url=FORUM_URL, external_id=topic_id)
        topic = BlizzardForumTopic.objects.select_for_update().get(pk=topic.pk)
        # A successful concurrent response newer than this attempt wins.
        if topic.last_observed_at and topic.last_observed_at >= attempted_at:
            return
        if topic.metadata_retry_at is None or topic.metadata_retry_at <= attempted_at:
            topic.metadata_retry_at = attempted_at + timedelta(seconds=METADATA_RETRY_SECONDS)
            topic.save(update_fields=['metadata_retry_at'])


class ForumMetadataCollector:
    """One scan's exact-URL responses, shared by metadata and body consumers."""

    def __init__(self, req, write_guard=None):
        self.req = req
        self.write_guard = write_guard
        self.successful_topics = {}
        self.responses = {}
        self.blocked_urls = set()
        self.last_error = ''

    def check_lease(self):
        if self.write_guard:
            with transaction.atomic():
                self.write_guard()

    def html_for_url(self, url):
        self.check_lease()
        topic_id = topic_id_from_url(url)
        if url in self.blocked_urls:
            raise TopicIdentityMismatch('response_topic_mismatch')
        if url in self.responses:
            return self.responses[url]
        topic = BlizzardForumTopic.objects.filter(forum_url=FORUM_URL, external_id=topic_id).first()
        now = timezone.now()
        if topic and topic.metadata_retry_at and topic.metadata_retry_at > now:
            return ''
        # Memoise all outcomes, including 403, for this scan. No hidden retry here.
        self.responses[url] = ''
        response = self.req.get(url, 'Response', 0, '', headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
            'Referer': 'https://us.forums.blizzard.com/',
        })
        # The request can outlive its owner, including a fresh-category body fetch.
        self.check_lease()
        if response is None or getattr(response, 'status_code', 0) != 200:
            raise ValueError('http_status')
        final_url = getattr(response, 'url', '')
        if final_url and topic_id_from_url(final_url) != topic_id:
            self.blocked_urls.add(url)
            raise TopicIdentityMismatch('redirect_topic_mismatch')
        text = response.text or ''
        try:
            parse_topic_metadata(text, url)
        except TopicIdentityMismatch:
            self.blocked_urls.add(url)
            raise
        except ValueError:
            # Classification failures must not discard otherwise valid body HTML.
            pass
        self.responses[url] = text
        return text

    def ensure(self, article, force=False):
        self.last_error = ''
        self.check_lease()
        attempted_at = timezone.now()
        try:
            if article.source != 'blizzard_tracker':
                return False
            topic_id = topic_id_from_url(article.url)
        except ValueError as exc:
            self.last_error = type(exc).__name__
            return False
        topic = BlizzardForumTopic.objects.select_related('category').filter(
            forum_url=FORUM_URL, external_id=topic_id).first()
        if topic and topic.metadata_retry_at and topic.metadata_retry_at > attempted_at:
            self.last_error = 'metadata_cooldown'
            return False
        if topic_id in self.successful_topics:
            attach_topic(article, self.successful_topics[topic_id], write_guard=self.write_guard)
            return True
        if (not force and topic and topic.category_id and topic.last_observed_at
                and (attempted_at - topic.last_observed_at).total_seconds() < METADATA_TTL_SECONDS):
            attach_topic(article, topic, write_guard=self.write_guard)
            self.successful_topics[topic_id] = topic
            return True
        try:
            html_text = self.html_for_url(article.url)
            if not html_text:
                raise ValueError('empty_response')
            topic = persist_topic_metadata(article, html_text, article.url,
                                           observed_at=attempted_at, write_guard=self.write_guard)
            self.successful_topics[topic_id] = topic
            return True
        except MonitorTaskLeaseLost:
            raise
        except Exception as exc:
            self.last_error = type(exc).__name__
            record_metadata_failure(topic_id, attempted_at, write_guard=self.write_guard)
            return False
