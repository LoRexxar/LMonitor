"""Dashboard 与 Portal 共用的攻略组件渲染。"""
import copy
from urllib.parse import urlencode

from botend.constants.wow import canonical_class_spec
from botend.services.class_guide_content import render_references, safe_url, walk_blocks, talent_version_for
from botend.services.class_guide_tools import gear_tool_url


def render_blocks(blocks, refs, guide):
    result = copy.deepcopy(blocks)
    identity = canonical_class_spec(guide.class_name, guide.spec_name) or ('', '')
    version = talent_version_for(guide.game_version)
    for block in walk_blocks(result):
        for field in ('html', 'title'):
            block[field] = render_references(block.get(field, ''), refs)
        data = block.setdefault('data', {})
        if block['type'] == 'priority' and data.get('decoded') and not block.get('children'):
            from botend.services.class_guide_icy_format import priority_html
            from botend.services.class_guide_codec import STATS
            decoded = data['decoded']
            labels = decoded.get('stat_labels') or [STATS.get(stat, f'属性 {stat}') for stat in decoded.get('stats', [])]
            if len(labels) > 1 and len(decoded.get('relation', [])) == len(labels) - 1:
                block['html'] = priority_html(labels, decoded['relation'])
        data['tool_url'] = ''
        if block['type'] == 'talents' and data.get('code'):
            data['tool_url'] = '/portal/talents/?' + urlencode({'class': identity[0], 'spec': identity[1],
                'code': data['code'], 'version': data.get('talent_version') or (version.key if version else guide.game_version)})
        elif block['type'] == 'gear' and data.get('decoded'):
            data['tool_url'] = gear_tool_url(data['decoded'], guide, refs)
        if data.get('steps'):
            data['steps'] = [render_references(str(step), refs) for step in data['steps']]
        data['source_url'] = safe_url(data.get('source_url', ''))
    if 'icy-veins.com' in (guide.source_url or ''):
        from botend.services.class_guide_icy_format import restore_icy_format
        restore_icy_format(result, guide)
    from botend.services.class_guide_presentation import prepare_guide_presentation
    return prepare_guide_presentation(result, guide)
