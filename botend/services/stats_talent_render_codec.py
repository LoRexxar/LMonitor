"""只保存天赋树事实和统计差异；重复的布局与渲染集合在读取时生成。"""

RECIPE = '_stats_render_recipe'


def _encode_delta(delta):
    schemas, indices = [], {}

    def encode(change):
        if change is None:
            return None
        if 'set' in change:
            return [0, change['set']]
        if 'items' in change:
            return [2, [[index, encode(item)] for index, item in change['items']]]
        keys = tuple(change['fields'])
        if keys not in indices:
            indices[keys] = len(schemas)
            schemas.append(list(keys))
        return [1, indices[keys], [encode(item) for item in change['fields'].values()], change['remove']]

    payload = encode(delta)
    return {'schemas': schemas, 'change': payload}


def _decode_delta(encoded):
    if not isinstance(encoded, dict) or set(encoded) != {'schemas', 'change'}:
        raise ValueError('统计差异字段表无效')
    schemas = encoded['schemas']
    if not isinstance(schemas, list) or any(not isinstance(row, list)
            or any(not isinstance(key, str) for key in row) or len(set(row)) != len(row) for row in schemas):
        raise ValueError('统计差异列名无效')

    def decode(change):
        if change is None:
            return None
        if not isinstance(change, list) or len(change) < 2 or type(change[0]) is not int:
            raise ValueError('统计差异指令无效')
        if change[0] == 0 and len(change) == 2:
            return {'set': change[1]}
        if change[0] == 1 and len(change) == 4:
            if (type(change[1]) is not int or not 0 <= change[1] < len(schemas)
                    or not isinstance(change[2], list) or len(change[2]) != len(schemas[change[1]])):
                raise ValueError('统计差异字段引用越界')
            return {'fields': dict(zip(schemas[change[1]], [decode(item) for item in change[2]])),
                    'remove': change[3]}
        if change[0] == 2 and len(change) == 2 and isinstance(change[1], list):
            if any(not isinstance(item, list) or len(item) != 2 for item in change[1]):
                raise ValueError('统计列表差异无效')
            return {'items': [[index, decode(item)] for index, item in change[1]]}
        raise ValueError('未知统计差异指令')

    return decode(encoded['change'])


def _difference(before, after):
    if before is after or (not isinstance(before, (dict, list))
                           and type(before) is type(after) and before == after):
        return None
    if isinstance(before, dict) and isinstance(after, dict):
        changes = {}
        for key, value in after.items():
            delta = _difference(before[key], value) if key in before else {'set': value}
            if delta is not None:
                changes[key] = delta
        removed = [key for key in before if key not in after]
        return {'fields': changes, 'remove': removed} if changes or removed else None
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        changes = []
        for index, (old, new) in enumerate(zip(before, after)):
            delta = _difference(old, new)
            if delta is not None:
                changes.append([index, delta])
        return {'items': changes} if changes else None
    return {'set': after}


def _apply(value, delta):
    if delta is None:
        return value
    if not isinstance(delta, dict):
        raise ValueError('天赋统计差异格式无效')
    if set(delta) == {'set'}:
        return delta['set']
    if set(delta) == {'fields', 'remove'} and isinstance(value, dict):
        if not isinstance(delta['fields'], dict) or not isinstance(delta['remove'], list):
            raise ValueError('天赋统计字段差异无效')
        result = dict(value)
        for key in delta['remove']:
            if not isinstance(key, str) or key not in result:
                raise ValueError('天赋统计删除字段无效')
            del result[key]
        for key, change in delta['fields'].items():
            if key not in result and (not isinstance(change, dict) or set(change) != {'set'}):
                raise ValueError('天赋统计新增字段无效')
            result[key] = _apply(result.get(key), change)
        return result
    if set(delta) == {'items'} and isinstance(value, list) and isinstance(delta['items'], list):
        result, seen = list(value), set()
        for item in delta['items']:
            if (not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int
                    or not 0 <= item[0] < len(result) or item[0] in seen):
                raise ValueError('天赋统计列表差异越界')
            seen.add(item[0])
            result[item[0]] = _apply(result[item[0]], item[1])
        return result
    raise ValueError('未知天赋统计差异格式')


def _references(model):
    nodes = model['nodes']
    indices = {node['node_key']: index for index, node in enumerate(nodes)}
    if len(indices) != len(nodes):
        raise ValueError('天赋渲染节点身份重复')
    trees = []
    for tree in model['trees']:
        refs = []
        for node in tree['nodes']:
            index = indices[node['node_key']]
            if nodes[index] != node:
                raise ValueError('天赋渲染节点内容不一致')
            refs.append(index)
        trees.append({**tree, 'nodes': refs})
    return {**model, 'trees': trees}


def _base(tree_set, build_state):
    from botend.wow.talents.models import TalentTreeSetModel, TalentBuildStateModel
    from botend.wow.talents.render import build_talent_render_model
    return build_talent_render_model(tree_set=TalentTreeSetModel(**tree_set),
        build_state=TalentBuildStateModel(**build_state)).to_dict()


def pack_render_recipe(detail):
    popularity = detail.get('talent_popularity_tree')
    model = popularity.get('render_model') if isinstance(popularity, dict) else None
    if not isinstance(model, dict) or 'tree_set' not in model or 'build_state' not in model:
        return detail, False
    try:
        base = _base(model['tree_set'], model['build_state'])
        # 未知扩展及不具备稳定重建契约的旧结构，继续走旧格式兼容路径。
        if base['tree_set'] != model['tree_set'] or base['build_state'] != model['build_state']:
            return detail, False
        delta = _difference(_references(base), _references(model))
        recipe = {RECIPE: 2, 'tree_set': model['tree_set'],
                  'build_state': model['build_state'], 'delta': _encode_delta(delta)}
        restored = unpack_render_recipe({**detail, 'talent_popularity_tree': {
            **popularity, 'render_model': recipe}})
        if restored != detail:
            return detail, False
        return {**detail, 'talent_popularity_tree': {**popularity, 'render_model': recipe}}, True
    except (KeyError, TypeError, ValueError, AttributeError):
        return detail, False


def unpack_render_recipe(detail):
    popularity = detail['talent_popularity_tree']
    recipe = popularity['render_model']
    if (not isinstance(recipe, dict) or set(recipe) != {RECIPE, 'tree_set', 'build_state', 'delta'}
            or type(recipe[RECIPE]) is not int or recipe[RECIPE] not in (1, 2)):
        raise ValueError('未知天赋渲染重建格式')
    delta = _decode_delta(recipe['delta']) if recipe[RECIPE] == 2 else recipe['delta']
    model = _apply(_references(_base(recipe['tree_set'], recipe['build_state'])), delta)
    nodes = model['nodes']
    trees = []
    for tree in model['trees']:
        if any(type(index) is not int or not 0 <= index < len(nodes) for index in tree['nodes']):
            raise ValueError('天赋树节点引用越界')
        trees.append({**tree, 'nodes': [nodes[index] for index in tree['nodes']]})
    return {**detail, 'talent_popularity_tree': {**popularity,
            'render_model': {**model, 'trees': trees}}}
