"""统计落盘去重：方案只保存一次，页面读取时恢复原有结构。"""
from collections import Counter


MARKER = '_stats_storage'


def _pack_builds(detail):
    """仅折叠能严格证明相等的别名；不裁剪样本、方案或差异。"""
    popularity = detail.get('talent_build_popularity')
    if not isinstance(popularity, dict) or MARKER in detail:
        return detail
    builds = popularity.get('builds')
    families = popularity.get('families')
    if not isinstance(builds, list) or not builds or not isinstance(families, list):
        return detail
    try:
        by_key = {row.get('state_key'): index for index, row in enumerate(builds)}
    except (TypeError, AttributeError):
        return detail
    if len(by_key) != len(builds) or None in by_key:
        return detail

    def reference(row):
        index = by_key[row['state_key']]
        if builds[index] != row:
            raise ValueError('方案别名内容不同')
        return index

    try:
        compact_families = [{**family,
            'builds': [reference(row) for row in family['builds']],
            'representative': reference(family['representative'])} for family in families]
        primary = reference(popularity['primary_template'])
        template = families.index(popularity['template_family'])
    except (ValueError, KeyError, TypeError):
        return detail
    return {**detail, MARKER: 1, 'talent_build_popularity': {
        **popularity, 'families': compact_families,
        'primary_template': primary, 'template_family': template}}


def _unpack_builds(detail):
    """兼容旧完整文件；恢复后的别名共享对象，避免再次复制大块内容。"""
    if MARKER not in detail:
        return detail
    if detail[MARKER] != 1:
        raise ValueError('未知统计存储版本')
    popularity = detail['talent_build_popularity']
    builds = popularity['builds']

    def resolve(index, rows):
        if type(index) is not int or not 0 <= index < len(rows):
            raise ValueError('统计存储引用越界')
        return rows[index]

    families = [{**family, 'builds': [resolve(i, builds) for i in family['builds']],
                 'representative': resolve(family['representative'], builds)}
                for family in popularity['families']]
    return {**{key: value for key, value in detail.items() if key != MARKER},
            'talent_build_popularity': {**popularity, 'families': families,
                'primary_template': resolve(popularity['primary_template'], builds),
                'template_family': resolve(popularity['template_family'], families)}}


TEXT_REF = '_stats_text_ref'


def _pack_rows(rows):
    """节点字段名按结构保存一次，行保留全部值及原有顺序。"""
    schemas, indices, values = [], {}, []
    for row in rows:
        columns = tuple(row)
        if columns not in indices:
            indices[columns] = len(schemas)
            schemas.append(list(columns))
        values.append([indices[columns], *row.values()])
    return {'schemas': schemas, 'rows': values}


def _unpack_rows(table):
    schemas = table['schemas']
    if not isinstance(schemas, list) or any(not isinstance(keys, list)
            or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)
            for keys in schemas):
        raise ValueError('统计节点字段表无效')
    result = []
    for row in table['rows']:
        if (not isinstance(row, list) or not row or type(row[0]) is not int
                or not 0 <= row[0] < len(schemas) or len(row) != len(schemas[row[0]]) + 1):
            raise ValueError('统计节点行无效')
        result.append(dict(zip(schemas[row[0]], row[1:])))
    return result


def _pack_render(detail):
    popularity = detail.get('talent_popularity_tree')
    model = popularity.get('render_model') if isinstance(popularity, dict) else None
    if not isinstance(model, dict) or not model.get('nodes') or not model.get('trees'):
        return detail, False
    try:
        nodes = model['nodes']
        indices = {row['node_key']: index for index, row in enumerate(nodes)}
        if len(indices) != len(nodes):
            return detail, False
        panels = model['layout']['panels']
        trees = []
        for tree in model['trees']:
            refs = [indices[row['node_key']] for row in tree['nodes']]
            if any(nodes[index] != row for index, row in zip(refs, tree['nodes'])):
                return detail, False
            panel = tree['panel']
            panel_index = panels.index(panel) if panel is not None else None
            paths = tree['paths']
            if not isinstance(paths, list):
                return detail, False
            if panel_index is not None and paths == panels[panel_index]['paths']:
                paths = panel_index
            trees.append({**tree, 'nodes': refs, 'panel': panel_index, 'paths': paths})
        layout = {**model['layout'], 'panels': [
            {**panel, 'nodes': _pack_rows(panel['nodes'])} for panel in panels]}
        tree_set = {**model['tree_set'], 'trees': [
            {**tree, 'nodes': _pack_rows(tree['nodes'])} for tree in model['tree_set']['trees']]}
        compact = {**model, 'nodes': _pack_rows(nodes), 'trees': trees,
                   'layout': layout, 'tree_set': tree_set}
    except (ValueError, KeyError, TypeError, AttributeError):
        return detail, False
    return {**detail, 'talent_popularity_tree': {**popularity, 'render_model': compact}}, True


def _unpack_render(detail):
    popularity = detail['talent_popularity_tree']
    model = popularity['render_model']
    nodes = _unpack_rows(model['nodes'])
    panels = [{**panel, 'nodes': _unpack_rows(panel['nodes'])} for panel in model['layout']['panels']]

    def ref(index, rows):
        if type(index) is not int or not 0 <= index < len(rows):
            raise ValueError('统计渲染引用越界')
        return rows[index]

    trees = [{**tree, 'nodes': [ref(i, nodes) for i in tree['nodes']],
              'panel': ref(tree['panel'], panels) if tree['panel'] is not None else None,
              'paths': ref(tree['paths'], panels)['paths'] if type(tree['paths']) is int else tree['paths']}
             for tree in model['trees']]
    restored = {**model, 'nodes': nodes, 'trees': trees,
        'layout': {**model['layout'], 'panels': panels},
        'tree_set': {**model['tree_set'], 'trees': [
            {**tree, 'nodes': _unpack_rows(tree['nodes'])} for tree in model['tree_set']['trees']]}}
    return {**detail, 'talent_popularity_tree': {**popularity, 'render_model': restored}}


def _pack_local(detail):
    """长描述按内容驻留一次；渲染层的多套节点不再重复保存相同文本。"""
    if MARKER in detail:
        return detail
    packed = _pack_builds(detail)
    packed, render = _pack_render(packed)
    counts = Counter()
    collision = False

    def count(value):
        nonlocal collision
        if isinstance(value, str) and len(value) >= 128:
            counts[value] += 1
        elif isinstance(value, dict):
            if set(value) == {TEXT_REF}:
                collision = True
            for child in value.values():
                count(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                count(child)

    count(packed)
    texts = [text for text, repeats in counts.items() if repeats > 1]
    if collision:
        return _pack_builds(detail)
    if not texts and not render:
        return packed
    indices = {text: index for index, text in enumerate(texts)}

    def replace(value):
        if isinstance(value, str):
            return {TEXT_REF: indices[value]} if value in indices else value
        if isinstance(value, dict):
            return {key: replace(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [replace(child) for child in value]
        return value

    result = replace({key: value for key, value in packed.items() if key != MARKER})
    result[MARKER] = {'schema': 3, 'builds': packed.get(MARKER) == 1, 'render': render, 'texts': texts}
    return result


def _unpack_local(detail):
    meta = detail.get(MARKER)
    if not isinstance(meta, dict):
        return _unpack_builds(detail)
    if (meta.get('schema') not in (2, 3) or type(meta.get('builds')) is not bool
            or (meta['schema'] == 3 and type(meta.get('render')) is not bool)
            or not isinstance(meta.get('texts'), list)
            or any(not isinstance(text, str) for text in meta['texts'])):
        raise ValueError('未知统计文本存储格式')
    texts = meta['texts']

    def restore(value):
        if isinstance(value, dict):
            if set(value) == {TEXT_REF}:
                index = value[TEXT_REF]
                if type(index) is not int or not 0 <= index < len(texts):
                    raise ValueError('统计文本引用越界')
                return texts[index]
            return {key: restore(child) for key, child in value.items()}
        if isinstance(value, list):
            return [restore(child) for child in value]
        return value

    result = restore({key: value for key, value in detail.items() if key != MARKER})
    if meta.get('render'):
        result = _unpack_render(result)
    if meta['builds']:
        result[MARKER] = 1
        result = _unpack_builds(result)
    return result


def pack_detail(detail, *, catalog_cache=None):
    """统计事实保留在文件中，重复渲染结构重建，已有展示资料引用数据库。"""
    if MARKER in detail:
        return detail
    from .stats_talent_render_codec import pack_render_recipe
    from .stats_talent_references import pack_metadata_references
    from .stats_player_references import pack_players
    compact, recipe = pack_render_recipe(detail)
    compact, metadata = pack_metadata_references(compact, catalog_cache=catalog_cache)
    compact, players = pack_players(compact)
    if recipe:
        popularity = compact['talent_popularity_tree']
        model = popularity['render_model']
        compact = {**compact, 'talent_popularity_tree': {**popularity,
            'render_model': {**model, 'tree_set': {**model['tree_set'], 'trees': [
                {**tree, 'nodes': _pack_rows(tree['nodes'])} for tree in model['tree_set']['trees']]}}}}
    compact = _pack_local(compact)
    if not recipe and metadata is None and not players:
        return compact
    inner = compact.get(MARKER)
    return {**compact, MARKER: {'schema': 4, 'local': inner,
                               'recipe': recipe, 'metadata': metadata, 'players': players}}


def unpack_detail(detail):
    meta = detail.get(MARKER)
    if not isinstance(meta, dict) or meta.get('schema') != 4:
        return _unpack_local(detail)
    if set(meta) != {'schema', 'local', 'recipe', 'metadata', 'players'} or type(meta['recipe']) is not bool:
        raise ValueError('未知统计引用存储格式')
    from .stats_talent_render_codec import unpack_render_recipe
    from .stats_talent_references import unpack_metadata_references
    from .stats_player_references import unpack_players
    result = {key: value for key, value in detail.items() if key != MARKER}
    if meta['local'] is not None:
        result[MARKER] = meta['local']
        result = _unpack_local(result)
    if meta['recipe']:
        popularity = result['talent_popularity_tree']
        model = popularity['render_model']
        result = {**result, 'talent_popularity_tree': {**popularity,
            'render_model': {**model, 'tree_set': {**model['tree_set'], 'trees': [
                {**tree, 'nodes': _unpack_rows(tree['nodes'])} for tree in model['tree_set']['trees']]}}}}
    if meta['players']:
        result = unpack_players(result, meta['players'])
    if meta['metadata'] is not None:
        result = unpack_metadata_references(result, meta['metadata'])
    if meta['recipe']:
        result = unpack_render_recipe(result)
    return result
