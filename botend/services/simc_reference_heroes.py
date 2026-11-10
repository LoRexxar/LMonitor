"""专精资料页的英雄天赋选择与现行方案筛选。"""

from django.http import Http404

from botend.constants.hero_talents import (
    HERO_SUBTREE_ID_TO_NAME, hero_subtree_name_zh, spec_hero_subtree_ids,
)
from botend.models import SimcTalentString
from botend.services.simc_reference_display import reference_trees


def hero_selection(params, spec):
    allowed = spec_hero_subtree_ids(spec['class_name'], spec['spec_name'])
    values = params.getlist('hero') if hasattr(params, 'getlist') else params.get('hero', [])
    if isinstance(values, str):
        values = [values] if values else []
    try:
        selected = {int(value) for value in values}
    except (ValueError, TypeError):
        raise Http404('英雄天赋不存在')
    if not selected.issubset(allowed):
        raise Http404('英雄天赋不属于当前专精')
    local = params.get('apl_hero', '')
    if local in ('', 'inherit'):
        apl_selected = selected
        local = ''
    elif local == 'all':
        apl_selected = set()
    else:
        try:
            sid = int(local)
        except (ValueError, TypeError):
            raise Http404('英雄天赋不存在')
        if sid not in allowed:
            raise Http404('英雄天赋不属于当前专精')
        apl_selected = {sid}
        local = str(sid)
    options = [{'id': str(sid), 'name': hero_subtree_name_zh(HERO_SUBTREE_ID_TO_NAME[sid]),
                'selected': sid in selected} for sid in sorted(allowed)]
    apl_id = next(iter(apl_selected)) if len(apl_selected) == 1 else None
    return {'options': options, 'ids': selected, 'apl_value': local, 'apl_id': apl_id,
            'apl_label': hero_subtree_name_zh(HERO_SUBTREE_ID_TO_NAME[apl_id]) if apl_id else '',
            'label': '、'.join(option['name'] for option in options if option['selected'])}


def tree_hero_ids(display):
    return {int(node.get('db2_subtree_id') or 0)
            for tree in display.get('trees', []) if tree.get('tree_type') == 'hero'
            for node in tree['nodes'] if node.get('selected') and node.get('db2_subtree_id')}


def filter_talent_rows(rows, selected, spec, version):
    """优先已有的英雄树归属；旧记录缺少归属时只读解析当前专精的代码。"""
    if not selected:
        return rows, {}
    name_ids = {hero_subtree_name_zh(name): sid for sid, name in HERO_SUBTREE_ID_TO_NAME.items()}
    memberships, displays = {}, {}
    unknown = []
    for row in rows:
        ids = {name_ids[hero_subtree_name_zh(name)] for name in row.get('hero_talent_names') or []
               if hero_subtree_name_zh(name) in name_ids}
        if ids:
            memberships[row['id']] = ids
        else:
            unknown.append(row['id'])
    if version and unknown:
        for row in SimcTalentString.objects.filter(
                pk__in=unknown, is_active=True, is_selectable=True, is_system=True,
                owner_user_id__isnull=True).values('id', 'talent'):
            display = reference_trees(row['talent'], spec, version)
            displays[row['id']] = display
            memberships[row['id']] = tree_hero_ids(display)
    return [row for row in rows if memberships.get(row['id'], set()) & selected], displays


def filter_tree_panels(display, selected):
    if not selected:
        return display
    trees = [tree for tree in display['trees'] if tree.get('tree_type') != 'hero' or
             any(node.get('db2_subtree_id') in selected for node in tree['nodes'])]
    model = display.get('render_model')
    return {**display, 'trees': trees, **({'render_model': {
        **model, 'trees': trees, 'nodes': [node for tree in trees for node in tree['nodes']],
    }} if model else {})}
