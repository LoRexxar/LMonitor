"""把来源中的选项卡展开为连续章节，保留其余组件与原始编号。"""
import copy


def expand_tab_sections(blocks, on_change=None):
    def changed(block, level=None):
        if on_change:
            on_change(block, level)

    def visit(rows, parent=1, minimum=1):
        result, current, offset, tab_index = [], parent, None, 0
        for original in rows:
            block = copy.deepcopy(original)
            kind = block['type']
            if kind in {'tabs', 'tab'}:
                if kind == 'tab':
                    tab_index += 1
                title = block.get('title') or ('方案 {}'.format(tab_index) if kind == 'tab' else '')
                level = min(6, current + 1) if title else current
                changed(block, level if title else None)
                if title:
                    result.append({**block, 'type': 'heading', 'title': title,
                                   'data': {**block.get('data', {}), 'level': level}, 'children': [], 'html': ''})
                if block.get('html'):
                    result.append({'id': block['id'] + '-body', 'type': 'html', 'html': block['html'], 'children': []})
                result.extend(visit(block.get('children', []), level, min(6, level + 1)))
            else:
                if kind == 'heading':
                    old_level = int(block.get('data', {}).get('level', 2))
                    if offset is None:
                        offset = max(0, minimum - old_level)
                    current = min(6, max(minimum, old_level + offset))
                    if current != old_level:
                        changed(block, current)
                    block.setdefault('data', {})['level'] = current
                block['children'] = visit(block.get('children', []), current, minimum)
                result.append(block)
        return result

    return visit(blocks)
