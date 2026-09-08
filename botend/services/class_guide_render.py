"""Dashboard 与 Portal 共用的攻略修订选择和组件渲染。"""
import copy
from urllib.parse import urlencode

from botend.constants.wow import canonical_class_spec
from botend.services.class_guide_content import render_references, safe_url, walk_blocks, talent_version_for
from botend.services.class_guide_tools import gear_tool_url


def selected_revision(guide):
    newest = guide.revisions.first()
    if newest and newest.audit.get('manual_conflict'):
        return guide.revisions.filter(origin='manual').first() or newest
    return newest


def render_blocks(blocks, refs, guide):
    result = copy.deepcopy(blocks)
    identity = canonical_class_spec(guide.class_name, guide.spec_name) or ('', '')
    version = talent_version_for(guide.game_version)
    for block in walk_blocks(result):
        for field in ('html', 'title'):
            block[field] = render_references(block.get(field, ''), refs)
        data = block.setdefault('data', {})
        data['tool_url'] = ''
        if block['type'] == 'talents' and data.get('code'):
            data['tool_url'] = '/portal/talents/?' + urlencode({'class': identity[0], 'spec': identity[1],
                'code': data['code'], 'version': data.get('talent_version') or (version.key if version else guide.game_version)})
        elif block['type'] == 'gear' and data.get('decoded'):
            data['tool_url'] = gear_tool_url(data['decoded'], guide, refs)
        if data.get('steps'):
            data['steps'] = [render_references(str(step), refs) for step in data['steps']]
        data['source_url'] = safe_url(data.get('source_url', ''))
    return result
