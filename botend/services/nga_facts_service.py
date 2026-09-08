"""NGA facts from observed HTML, never collector labels or inferred page counts."""
import json
import re
from urllib.parse import urljoin, urlsplit, parse_qs
from bs4 import BeautifulSoup
from django.utils import timezone

LEGACY_AUTHORS = {'nga前瞻区', 'nga水区'}

def decode_page(content):
    if isinstance(content, bytes):
        match = re.search(br'charset\s*=\s*["\']?([\w-]+)', content[:4096], re.I)
        encoding = match.group(1).decode() if match else 'gb18030'
        try:
            return content.decode(encoding, 'replace')
        except LookupError:
            return content.decode('gb18030', 'replace')
    return str(content or '')

def board_facts(soup):
    # Breadcrumb's last forum, not the parent forum or a collector nickname.
    result = {}
    for a in soup.select('a.nav_link[href*="thread.php?fid="]'):
        fid = parse_qs(urlsplit(a['href']).query).get('fid', [''])[0]
        name = a.get_text(' ', strip=True)
        if re.fullmatch(r'-?\d+', fid) and name:
            result = {'nga_board_id': fid, 'nga_board_name': name[:255]}
    return result

def parse_listing(content):
    soup = BeautifulSoup(decode_page(content), 'html.parser')
    board = board_facts(soup)
    rows = []
    for row in soup.select('#topicrows tbody'):
        topic = row.select_one('a.topic[href]')
        if not topic:
            continue
        match = re.search(r'(?:\?|&)tid=(\d+)', topic['href'])
        if not match:
            continue
        facts = dict(board)
        author = row.select_one('a.author')
        if author and author.get_text(strip=True):
            facts['author'] = author.get_text(strip=True)[:255]
        replies = row.select_one('a.replies')
        if replies and re.fullmatch(r'\d+', replies.get_text(strip=True)):
            facts.update(reply_count=int(replies.get_text(strip=True)), nga_replies_updated_at=timezone.now())
        rows.append({'url': 'https://bbs.nga.cn/read.php?tid=' + match.group(1),
                     'title': topic.get_text(' ', strip=True), 'facts': facts})
    return rows

def parse_main_post(content):
    raw = decode_page(content)
    soup = BeautifulSoup(raw, 'html.parser')
    main = soup.select_one('#postcontent0')
    if main is None:
        return {}
    facts = board_facts(soup)
    facts['content'] = main.decode_contents().strip()
    author = soup.select_one('#postauthor0')
    if author:
        name = author.get_text(strip=True)
        uid = parse_qs(urlsplit(author.get('href', '')).query).get('uid', [''])[0]
        match = re.search(r'commonui\.userInfo\.setAll\(\s*', raw)
        if not name and match:
            try:
                # Source JS strings contain literal tabs in user remark data.
                # Accept those controls without executing JavaScript.
                users, _ = json.JSONDecoder(strict=False).raw_decode(raw[match.end():])
                name = users.get(uid, {}).get('username', '')
            except (ValueError, TypeError, AttributeError):
                pass
        if name:
            facts['author'] = name[:255]
    # Verified source definition: https://img4.nga.cn/common_res/js_read.js?3587966
    # setDefault(fid,stid,tid,tAid,topicMiscBit1,punUsers,visit,mods,vote,
    #            customLevel,tType,tReplies,tLastTime,thisPagePosts)
    # assigns def.tReplies=tReplies (comment: 回复数). Fail closed on new versions
    # or non-JSON arguments; never eval JS or count visible floors/pages.
    if 'js_read.js?3587966' in raw:
        for script in soup.select('script'):
            match = re.search(r'commonui\.postArg\.setDefault\(\s*(.*?)\)[ \t]*(?:;|\r?\n|$)',
                              script.get_text(), re.S)
            if not match:
                continue
            try:
                args = json.loads('[' + match.group(1) + ']', strict=False)
                if len(args) == 14 and type(args[11]) is int and args[11] >= 0:
                    facts.update(reply_count=args[11], nga_replies_updated_at=timezone.now())
            except (ValueError, TypeError):
                pass
    return facts

def apply_facts(article, facts):
    allowed = {'author', 'nga_board_id', 'nga_board_name', 'reply_count', 'nga_replies_updated_at', 'content'}
    updates = {k: v for k, v in facts.items() if k in allowed and v is not None and (v != '' or k == 'reply_count')}
    if updates:
        for key, value in updates.items():
            setattr(article, key, value)
        article.save(update_fields=list(updates))
    return updates

def fetch_page(url, cookie=''):
    import requests
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in {'bbs.nga.cn', 'nga.178.com'}:
        raise ValueError('Untrusted NGA URL')
    # Never propagate requests exceptions: they can contain authentication headers.
    try:
        response = requests.get(url, headers={'Cookie': re.sub(r'[\r\n]', '', cookie or '').strip(),
                                'User-Agent': 'Mozilla/5.0'}, timeout=12, allow_redirects=False)
    except requests.RequestException:
        raise ValueError('NGA network request failed') from None
    if response.status_code != 200:
        raise ValueError('NGA HTTP %s' % response.status_code)
    return decode_page(response.content)
