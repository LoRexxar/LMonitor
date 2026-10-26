"""攻略阅读投影：保留原稿，只整理可确认的来源排版残留。"""
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from django.utils.html import strip_tags

from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_links import GuideLinkPolicy


NUMBER = re.compile(r'^\d+(?:\.\d+)*\.$')
CONTINUATION = re.compile(r'^[，。、；：！？）)\]】]|^(?:以及|或者|并且|从而|和|或|来|使|让|以|的|为|移除|获得)')
SOURCE_NAV_HOSTS = {'www.icy-veins.com', 'icy-veins.com', 'raider.io', 'www.raider.io'}


def _remove_source_navigation(soup, links):
    """去掉来源推广和孤立导航，正文内的链接只解除跳转，保留词句。"""
    for img in list(soup.find_all('img')):
        if urlparse(img.get('src', '')).path != '/images/common/raiderio-logo.svg':
            continue
        paragraph = img.find_parent('p')
        if paragraph is None or paragraph.get_text(strip=True) or paragraph.select('.guide-ref'):
            continue
        label = paragraph.find_next_sibling()
        if (isinstance(label, Tag) and label.name == 'p'
                and len(label.get_text(strip=True)) < 40
                and label.get_text(strip=True).endswith(('资源', 'Resources'))
                and not label.find(['a', 'img']) and not label.select('.guide-ref')):
            label.decompose()
        paragraph.decompose()
    for paragraph in list(soup.find_all('p')):
        anchors = paragraph.find_all('a', href=True)
        if (not anchors or paragraph.select('.guide-ref') or paragraph.find_parent('table')
                or paragraph.find(['code', 'pre'])
                or any(urlparse(a['href']).hostname not in SOURCE_NAV_HOSTS
                       or links.href(a['href']) for a in anchors)):
            continue
        # 只有链接及标点的段落属于导航；含说明、获取途径或操作正文的段落保留。
        outside = ''.join(str(part) for part in paragraph.find_all(string=True)
                          if part.find_parent('a') is None)
        if not outside.strip(' \t\r\n，、。；：|/·,.;:'):
            paragraph.decompose()


def _reference_only(node):
    return (isinstance(node, Tag) and node.name == 'p' and len(node.select('.guide-ref')) == 1
            and node.get_text(strip=True) == node.select_one('.guide-ref').get_text(strip=True))


def _merge_reference_paragraphs(soup):
    """仅连接未结束的句子与明确续句；独立技能序列保持原样。"""
    for node in list(soup.find_all('p')):
        if not node.parent or not _reference_only(node):
            continue
        before, after = node.find_previous_sibling(), node.find_next_sibling()
        if not (isinstance(before, Tag) and before.name == 'p' and not _reference_only(before)
                and isinstance(after, Tag) and after.name == 'p' and not _reference_only(after)):
            continue
        previous = before.get_text(strip=True)
        following = after.get_text(strip=True)
        if (not previous or re.search(r'[。！？!?；;：:.]$', previous)
                or not CONTINUATION.search(following)):
            continue
        for part in list(node.contents):
            before.append(part.extract())
        node.decompose()
        for part in list(after.contents):
            before.append(part.extract())
        after.decompose()


def _remove_heading_number(rows):
    for index, row in enumerate(rows):
        _remove_heading_number(row.get('children', []))
        if row['type'] != 'heading' or index == 0:
            continue
        previous = rows[index - 1]
        if previous['type'] != 'html' or not previous.get('html'):
            continue
        soup = BeautifulSoup(previous['html'], 'html.parser')
        last = next((node for node in reversed(soup.contents) if isinstance(node, Tag)), None)
        if last is None:
            continue
        number = last.get_text(strip=True)
        empty_list = (last.name == 'ol' and len(last.find_all('li')) == 1
                      and not number and not last.find(['img', 'table', 'pre', 'code']))
        if (last.name == 'p' and NUMBER.fullmatch(number)) or empty_list:
            row.setdefault('data', {})['source_number'] = number or str(last.get('start', 1)) + '.'
            last.decompose()
            previous['html'] = str(soup).strip()


def prepare_guide_presentation(blocks, guide):
    """输入已安全渲染的块；只增加站内定义的语义样式，不透传来源 CSS。"""
    icy = urlparse(guide.source_url or '').hostname in ('www.icy-veins.com', 'icy-veins.com')
    if icy:
        _remove_heading_number(blocks)
    links = GuideLinkPolicy(blocks, guide)
    source_ids, in_source = set(), False
    for block in blocks:
        if block['type'] == 'heading' and block.get('data', {}).get('level') == 2:
            in_source = strip_tags(block.get('title', '')).strip() == '来源与翻译说明'
        if in_source:
            source_ids.update(id(row) for row in walk_blocks([block]))
    for block in walk_blocks(blocks):
        if icy and block['type'] == 'details' and block.get('title'):
            block['title'] = re.sub(r'^(常见问题|FAQ)(?![：:\s])', r'\1：', block.get('title', ''))
        for field in ('html', 'title'):
            markup = block.get(field, '')
            if not markup:
                continue
            soup = BeautifulSoup(markup, 'html.parser', preserve_whitespace_tags={'pre', 'code'})
            if icy:
                _merge_reference_paragraphs(soup)
                if id(block) not in source_ids:
                    _remove_source_navigation(soup, links)
            links.clean(soup, source_note=id(block) in source_ids,
                        section=links.sections.get(block.get('id')))
            for strong in soup.find_all('strong'):
                if strong.get_text(strip=True).startswith(('适用条件：', '适用条件:')):
                    strong['class'] = ['guide-condition']
            for table in soup.find_all('table'):
                first_row = table.find('tr')
                if (not table.find('thead') and first_row
                        and first_row.find('th', recursive=False) and not first_row.find('td', recursive=False)):
                    head = soup.new_tag('thead')
                    head.append(first_row.extract())
                    table.insert(0, head)
                wrapper = soup.new_tag('div', attrs={'class': 'guide-table-scroll', 'role': 'region',
                                                     'aria-label': '攻略表格，可横向滚动', 'tabindex': '0'})
                table.wrap(wrapper)
                headers = [th.get_text(strip=True) for th in table.select('thead th')]
                if headers and headers[0] == '部位':
                    table['class'] = ['guide-gear-table']
                for th in table.select('thead th'):
                    th['scope'] = 'col'
                for th in table.select('tbody th'):
                    th['scope'] = 'row'
            for img in soup.find_all('img'):
                if img.find_parent(class_='guide-ref'):
                    continue
                path = urlparse(img.get('src', '')).path
                if '/large_icons/' in path or '/small_icons/' in path or '/hero-talents/' in path:
                    img['class'] = ['guide-inline-icon']
                    img['width'] = '24'
                    img['height'] = '24'
                elif '/images/common/' in path:
                    img['class'] = ['guide-source-logo']
            block[field] = str(soup)
        if block.get('data', {}).get('steps'):
            steps = []
            for step in block['data']['steps']:
                soup = BeautifulSoup(step, 'html.parser')
                links.clean(soup)
                steps.append(str(soup))
            block['data']['steps'] = steps
    return blocks


def guide_reading_structure(blocks):
    """主章节保持原顺序，目录按真实标题层级嵌套，所有锚点均保留。"""
    chapters, current = [], None
    for block in blocks:
        if block['type'] == 'heading' and block['data']['level'] == 2:
            current = {'id': block['id'], 'title': strip_tags(block['title']), 'blocks': [block],
                       'is_source': strip_tags(block['title']).strip() == '来源与翻译说明'}
            chapters.append(current)
        else:
            if current is None:
                current = {'id': '', 'title': '', 'blocks': [], 'is_source': False}
                chapters.append(current)
            current['blocks'].append(block)
    toc, stack = [], []
    for block in walk_blocks(blocks):
        if block['type'] != 'heading':
            continue
        node = {'id': block['id'], 'title': strip_tags(block['title']), 'level': block['data']['level'], 'children': []}
        while stack and stack[-1]['level'] >= node['level']:
            stack.pop()
        (stack[-1]['children'] if stack else toc).append(node)
        stack.append(node)
    return chapters, toc
