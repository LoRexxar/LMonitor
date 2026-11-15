"""Version-bound editorial layout corrections; original Markdown is never rewritten."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from bs4 import BeautifulSoup
from botend.services.class_guide_content import walk_blocks


@lru_cache(maxsize=1)
def _repairs():
    path = Path(__file__).resolve().parents[1] / 'data/class_guides/icy-layout-repairs.json'
    return json.loads(path.read_text())['articles']


def paragraph_identity(node):
    """Names are projections; identify preserved references by kind and ID, not label."""
    clone = BeautifulSoup(str(node), 'html.parser')
    for ref in clone.select('.guide-ref'):
        ref.replace_with('[[{}:{}]]'.format(ref.get('data-reference-kind'), ref.get('data-reference-id')))
    return clone.get_text().strip()


def restore_frozen_layout(blocks, guide):
    repair = _repairs().get(getattr(guide, 'slug', ''), {})
    if not repair or hashlib.sha256(guide.content_markdown.encode()).hexdigest() != repair['content_sha256']:
        return
    if hashlib.sha256(getattr(guide, 'source_markdown', '').encode()).hexdigest() != repair['source_sha256']:
        return
    by_id = {block['id']: block for block in walk_blocks(blocks)}
    for key, operations in repair['blocks'].items():
        block = by_id.get(key)
        if block is None:
            raise ValueError('Icy layout block identity changed: ' + key)
        soup = BeautifulSoup(block.get('html', ''), 'html.parser', preserve_whitespace_tags={'pre', 'code'})
        for operation in operations:
            expected = operation['paragraphs']
            candidates = []
            for node in soup.find_all('p'):
                if node.find_parent(['table', 'li', 'pre', 'code']):
                    continue
                nodes, current = [], node
                for value in expected:
                    if current is None or current.name != 'p' or paragraph_identity(current) != value:
                        break
                    nodes.append(current)
                    current = current.find_next_sibling()
                if len(nodes) == len(expected):
                    candidates.append(nodes)
            if len(candidates) != 1:
                raise ValueError('Icy layout paragraph identity changed: ' + key)
            nodes = candidates[0]
            if operation['kind'] == 'paragraph':
                for node in nodes[1:]:
                    left, right = nodes[0].get_text(), node.get_text()
                    if left and right and re.fullmatch(r'[A-Za-z0-9]{2}', left[-1] + right[0]):
                        nodes[0].append(' ')
                    for child in list(node.contents):
                        nodes[0].append(child.extract())
                    node.decompose()
            elif operation['kind'] == 'sequence':
                branches = operation['branches']
                if (not branches or branches[0]['start'] != 0 or branches[-1]['end'] != len(nodes)
                        or any(a['end'] != b['start'] for a, b in zip(branches, branches[1:]))):
                    raise ValueError('Icy opener branch coverage changed: ' + key)
                container = soup.new_tag('div', attrs={'class': 'guide-opener-branches'})
                nodes[0].insert_before(container)
                for branch in branches:
                    branch_nodes = nodes[branch['start']:branch['end']]
                    if sum(node.get_text(strip=True) in {'拉怪', 'Pull'} for node in branch_nodes) != 1:
                        raise ValueError('Icy opener branch must contain exactly one pull: ' + key)
                    wrapper = soup.new_tag('section', attrs={'class': 'guide-opener-branch'})
                    container.append(wrapper)
                    if branch['scope']:
                        if paragraph_identity(branch_nodes[0]) != branch['scope']:
                            raise ValueError('Icy opener scope changed: ' + key)
                        # This applicability label governs the whole independent list,
                        # not just its first action. Never flatten mutually exclusive lists.
                        wrapper.append(branch_nodes.pop(0).extract())
                    sequence = soup.new_tag('ol', attrs={'class': 'guide-opener-sequence', 'aria-label': '起手动作序列'})
                    wrapper.append(sequence)
                    pending = []
                    for node in branch_nodes:
                        is_condition = bool(node.find('strong')) and node.get_text(strip=True) == node.strong.get_text(strip=True) and node.strong.get_text(strip=True).startswith('适用条件')
                        if is_condition:
                            if not pending or str(pending[-1]) != str(node):
                                pending.append(node.extract())
                            else:
                                node.decompose()
                            continue
                        marker = bool(re.fullmatch(r'\d+(?:\.\d+)?s|\d+(?:\.\d+)?秒|x\d+|预读|预施法', node.get_text(strip=True)))
                        if marker and sequence.find('li') and not pending:
                            sequence.find_all('li', recursive=False)[-1].append(node.extract())
                            continue
                        item = soup.new_tag('li')
                        for condition in pending:
                            item.append(condition)
                        pending = []
                        item.append(node.extract())
                        sequence.append(item)
                    if pending:
                        raise ValueError('Icy opener has a condition without an action: ' + key)
            else:
                raise ValueError('Unknown Icy layout operation')
        block['html'] = str(soup)
