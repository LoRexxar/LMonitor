"""Maxroll 已授权攻略的目录发现与结构化转换。"""

import hashlib
import json
import re
from collections import Counter
from urllib.parse import urlparse, unquote, urljoin, urlencode

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from botend.constants.wow import CLASS_SPEC_MAP, CLASS_CN, SPEC_CN, canonical_class_spec
from botend.services.class_guide_content import clean_html, validate_blocks
from botend.services.class_guide_codec import decode_component, component_html
from botend.services.class_guide_authors import extract_author_profile
from botend.services.class_guide_sections import expand_tab_sections

CATALOG_URL = 'https://maxroll.gg/wow/class-guides'
CONTAINERS = {'advgb/adv-tabs': 'tabs', 'advgb/tab': 'tab', 'advgb/columns': 'columns',
              'core/columns': 'columns', 'advgb/column': 'column', 'core/column': 'column',
              'advgb/accordions': 'accordion', 'advgb/accordion-item': 'details',
              'maxroll/disclaimer': 'callout', 'core/group': 'group', 'core/gallery': 'group'}
HTML_TYPES = {'core/paragraph', 'core/list', 'core/list-item', 'advgb/list', 'core/quote', 'core/table', 'advgb/table', 'core/html'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class MaxrollClient:
    def __init__(self, request_client=None):
        self.request_client = request_client
        if request_client is not None:
            return
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'LMonitor-AuthorizedGuideSync/1.0'
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 502, 503, 504], allowed_methods=['GET'])
        self.session.mount('https://', HTTPAdapter(max_retries=retry))

    def fetch(self, url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != 'maxroll.gg' or not parsed.path.startswith('/wow/class-guides'):
            raise ValueError('来源必须是 Maxroll 魔兽攻略目录或正文')
        if self.request_client is not None:
            response = self.request_client.get(url, 'Response', 0, '',
                                               headers={'User-Agent': 'LMonitor-AuthorizedGuideSync/1.0'})
            if response is None or response is False or response.status_code != 200:
                raise ValueError('Maxroll 来源请求失败')
            final_url = urlparse(response.url)
            if final_url.scheme != 'https' or final_url.netloc != 'maxroll.gg' or not final_url.path.startswith('/wow/class-guides'):
                raise ValueError('来源重定向到攻略范围之外')
            if len(response.content) > 8000000:
                raise ValueError('来源页面超过 8 MB')
            return response.content.decode('utf-8')
        response = self.session.get(url, timeout=(10, 45), allow_redirects=False, stream=True)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('来源重定向或状态异常，需重新核对')
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 8000000:
                response.close()
                raise ValueError('来源页面超过 8 MB')
            chunks.append(chunk)
        return b''.join(chunks).decode('utf-8')

    def discover(self):
        markup = self.fetch(CATALOG_URL)
        urls = set(discover(markup))
        loaders = remix_context(markup).get('state', {}).get('loaderData', {})
        catalog = next((row['searchData'] for row in loaders.values()
                        if isinstance(row, dict) and 'searchData' in row), None)
        if catalog is None:
            raise ValueError('目录缺少分页信息，无法确认已发现全部攻略')
        response = catalog.get('initialSearchResponse') or {}
        if not isinstance(response.get('hits'), list) or not isinstance(response.get('estimatedTotalHits'), int) or response.get('offset', 0) != 0:
            raise ValueError('目录缺少有效的文章总数或首页数据，无法确认完整性')
        hits = response.get('hits') or []
        seen = {str(row['id']) for row in hits}
        total = int(response.get('estimatedTotalHits', len(hits)))
        offset = len(hits)
        while offset < total:
            config = loaders.get('root', {}).get('appContext', {}).get('search', {})
            response = self._catalog_search(config, offset)
            page = response.get('hits') or []
            new_ids = {str(row['id']) for row in page} - seen
            if not page or not new_ids:
                raise ValueError('目录分页提前结束或重复，拒绝将不完整目录标记为成功')
            seen.update(new_ids)
            hits.extend(page)
            offset += len(page)
            total = max(total, int(response.get('estimatedTotalHits', total)))
            if offset > 10000:
                raise ValueError('目录超过预期规模，请检查来源分页结构')
        if len(seen) < total:
            raise ValueError('目录结果存在重复或缺失，请重新同步完整目录')
        for row in hits:
            url = guide_url(row.get('permalink', ''), article=True)
            if url and not is_leveling(row):
                urls.add(url)
        # 目录分类也可标记练级，不能只依赖文章网址后缀。
        urls.difference_update(guide_url(row.get('permalink', ''), article=True) for row in hits if is_leveling(row))
        self.catalog_version = latest_source_version(hits)
        return sorted(urls)

    def _catalog_search(self, config, offset):
        if config.get('url', '').rstrip('/') != 'https://meilisearch-proxy.maxroll.gg' or config.get('index') != 'wp_posts_15' or not config.get('api_key'):
            raise ValueError('Maxroll 职业攻略目录搜索配置已变化')
        url = 'https://meilisearch-proxy.maxroll.gg/indexes/wp_posts_15/search'
        url += '?' + urlencode({'q': '', 'limit': 100, 'offset': offset,
                                'filter': 'taxonomies.category = "class-guides"',
                                'sort': 'post_modified_unix:desc'})
        options = {'headers': {'Authorization': 'Bearer ' + config['api_key']}}
        if self.request_client is not None:
            response = self.request_client.get(url, 'Response', 0, '', **options)
        else:
            response = self.session.get(url, timeout=(10, 45), allow_redirects=False, **options)
        if response is None or response is False or response.status_code != 200:
            raise ValueError('Maxroll 目录分页请求失败')
        if urlparse(response.url)._replace(query='') != urlparse(url)._replace(query='') or len(response.content) > 8000000:
            raise ValueError('Maxroll 目录分页地址或响应大小无效')
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('hits'), list):
            raise ValueError('Maxroll 目录分页缺少文章列表')
        return data

    def article(self, url):
        soup = BeautifulSoup(self.fetch(url), 'html.parser')
        script = next((s.string or s.get_text() for s in soup.find_all('script')
                       if (s.string or s.get_text()).lstrip().startswith('window.__remixContext =')), '')
        if not script:
            raise ValueError('缺少结构化正文，拒绝把空壳页面导入为完整文章')
        context = json.JSONDecoder().raw_decode(script.split('=', 1)[1].lstrip())[0]
        post = context.get('state', {}).get('loaderData', {}).get('branch-posts', {}).get('post')
        if not isinstance(post, dict) or not post.get('gutenbergBlock'):
            raise ValueError('来源结构已变化，缺少内容树')
        post['author_profile'] = extract_author_profile(soup, post.get('author') or {})
        return post


def remix_context(markup):
    soup = BeautifulSoup(markup, 'html.parser')
    script = next((s.get_text() for s in soup.find_all('script')
                   if s.get_text().lstrip().startswith('window.__remixContext =')), '')
    return json.JSONDecoder().raw_decode(script.split('=', 1)[1].lstrip())[0] if script else {}


def is_leveling(post):
    slug = post.get('slug') or urlparse(post.get('permalink', '')).path.rsplit('/', 1)[-1]
    metas = (post.get('taxonomies') or {}).get('metas') or []
    return bool(re.search(r'(?:^|-)level(?:l)?ing(?:-|$)', slug)) or 'leveling' in metas


def latest_source_version(posts):
    versions = {match[0] for post in posts for tag in post.get('tags') or []
                for match in re.finditer(r'\d+\.\d+(?:\.\d+)?', tag.get('name', ''))}
    return max(versions, key=lambda value: tuple(map(int, value.split('.'))), default='')


def guide_url(value, *, article=False):
    parsed = urlparse(urljoin(CATALOG_URL + '/', value))
    if parsed.scheme != 'https' or parsed.netloc != 'maxroll.gg' or not re.fullmatch(r'/wow/class-guides/[a-z0-9-]+/?', parsed.path):
        return None
    slug = parsed.path.rstrip('/').rsplit('/', 1)[-1]
    if is_leveling({'slug': slug}):
        return None
    if not article and not slug.endswith('-guide'):
        try:
            identity(slug)
        except ValueError:
            return None
    return 'https://maxroll.gg' + parsed.path.rstrip('/')


def discover(markup):
    soup = BeautifulSoup(markup, 'html.parser')
    urls = set()
    for a in soup.select('a[href]'):
        url = guide_url(a['href'])
        if url:
            urls.add(url)
    for loader in remix_context(markup).get('state', {}).get('loaderData', {}).values():
        if not isinstance(loader, dict):
            continue
        for row in loader.get('searchData', {}).get('initialSearchResponse', {}).get('hits', []):
            url = guide_url(row.get('permalink', ''), article=True)
            if url and not is_leveling(row):
                urls.add(url)
    if not urls:
        raise ValueError('目录没有发现非练级攻略，可能需要更新解析器')
    return sorted(urls)


def identity(slug):
    if is_leveling({'slug': slug}):
        raise ValueError('练级攻略不在同步范围内')
    guide_type = ('raid' if slug.endswith('-raid-guide') else 'mythic-plus' if re.search(r'-mythic(?:-plus)?-guide$', slug)
                  else 'pvp' if slug.endswith('-pvp-guide') else 'general')
    for class_name, specs in CLASS_SPEC_MAP.items():
        source_class = {'DeathKnight': 'death-knight', 'DemonHunter': 'demon-hunter'}.get(class_name, class_name.lower())
        for spec in specs:
            source_spec = 'beast-mastery' if spec == 'BeastMastery' else spec.lower()
            base = source_spec + '-' + source_class
            if slug == base or slug.startswith(base + '-'):
                return class_name, spec, guide_type
    raise ValueError('无法识别职业专精：' + slug)


def chinese_title(class_name, spec_name, guide_type):
    class_name, spec_name = canonical_class_spec(class_name, spec_name)
    return '{}{} · {}攻略'.format(SPEC_CN[spec_name], CLASS_CN[class_name],
                                 {'raid': '团本', 'mythic-plus': '大秘境', 'pvp': '玩家对战', 'general': '通用'}[guide_type])


def convert(post):
    refs, counts, unsupported = {}, Counter(), []
    def visit(rows, prefix='b'):
        result = []
        for index, raw in enumerate(rows):
            name = raw.get('blockName', '')
            counts[name] += 1
            block = {'id': '{}-{}'.format(prefix, index), 'type': 'html', 'source_type': name}
            attr = raw.get('attributes') or {}
            source_html = raw.get('innerHTML') or ''
            if name == 'core/spacer' or re.fullmatch(r'\s*\[inplace_ad\s+\d+\][\s\-–—]*', BeautifulSoup(source_html, 'html.parser').get_text()):
                continue
            if name in CONTAINERS:
                block['type'] = CONTAINERS[name]
                block['title'] = clean_html(attr.get('header') or attr.get('title') or '', refs)
                if name == 'core/gallery':
                    block['html'] = clean_html(source_html, refs)
            elif name in HTML_TYPES:
                block['html'] = clean_html(source_html, refs)
            elif name in ('core/heading', 'maxroll/title-separator'):
                block['type'] = 'heading'
                block['title'] = clean_html(attr.get('title') or BeautifulSoup(source_html, 'html.parser').get_text(' ', strip=True), refs)
                block['data'] = {'level': int(attr.get('level', 2))}
            elif name == 'core/code':
                block['type'] = 'code'
                block['data'] = {'code': BeautifulSoup(source_html, 'html.parser').get_text().strip()}
            elif name == 'core/image':
                block['type'] = 'image'
                block['html'] = clean_html(source_html, refs)
            elif name == 'core/separator':
                block['type'] = 'separator'
            elif name == 'game-blocks/difficulty-bar':
                block['type'] = 'rating'
                block['title'] = clean_html(attr.get('title', ''), refs)
                block['html'] = ''.join('<p><strong>{}</strong> · {}/5 · {}</p>'.format(
                    clean_html(r.get('title', ''), refs), int(r.get('difficulty', 0)), clean_html(r.get('subtitle', ''), refs)) for r in attr.get('content', []))
            elif name == 'cgb/block-changelog-block':
                block['type'] = 'changelog'
                block['title'] = '更新记录'
                block['html'] = ''.join('<p><strong>{}</strong> {}</p>'.format(clean_html(r.get('date', '')[:10]), clean_html(r.get('text', ''), refs)) for r in reversed(attr.get('changelogItems', [])))
            elif name == 'core/embed':
                url = str(attr.get('url', ''))
                match = re.search(r'/embed-tools/(talents|paperdoll|rotation|priority|timeline|simulation)=([^/?#]+)', url)
                if match:
                    block['type'] = {'paperdoll': 'gear'}.get(match[1], match[1])
                    block['data'] = {'source_url': url, 'source_code': unquote(match[2]), 'converted': match[1] == 'talents'}
                    if match[1] == 'talents':
                        block['data']['code'] = unquote(match[2]).replace('-', '+').replace('_', '/')
                    else:
                        try:
                            decoded = decode_component(match[1], unquote(match[2]))
                            block['data'].update(decoded=decoded, converted=True)
                            block['html'] = component_html(match[1], decoded)
                        except (ValueError, IndexError, UnicodeError) as exc:
                            unsupported.append({'id': block['id'], 'type': match[1], 'reason': str(exc)})
                    caption = BeautifulSoup(source_html, 'html.parser').find('figcaption')
                    block['title'] = clean_html(str(caption) if caption else '', refs)
                else:
                    block.update(type='unsupported', html=clean_html(source_html, refs), data={'source_url': url})
                    unsupported.append({'id': block['id'], 'type': name, 'reason': '未知嵌入模块'})
            else:
                block.update(type='unsupported', html=clean_html(source_html, refs))
                unsupported.append({'id': block['id'], 'type': name, 'reason': '未知内容块'})
            block['children'] = visit(raw.get('innerBlocks') or [], block['id'])
            result.append(block)
        return result
    blocks = validate_blocks(expand_tab_sections(visit(post['gutenbergBlock'])))
    return blocks, {'source_block_counts': dict(counts), 'unsupported': unsupported, 'source_refs': refs}
