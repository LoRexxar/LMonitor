"""聚合中的天赋展示文字引用现有数据库；按范围批量读取，绝不写库。"""
from collections import defaultdict

REFERENCE = '_stats_talent_meta'
TEXT_FIELDS = ('name', 'name_zh', 'icon', 'description', 'description_zh')
ROW_FIELDS = ('pk', 'talent_version_id', 'class_name', 'spec_name', 'tree_type',
              'node_id', 'talent_id', 'spell_id', 'display_spell_id', 'name', 'name_zh',
              'icon', 'row', 'column', 'max_points', 'parents_json', 'description',
              'description_zh', 'db2_subtree_id', 'db2_tree_id', 'db2_component_id',
              'flags', 'source')


def _scope(detail):
    popularity = detail.get('talent_popularity_tree') or {}
    if not isinstance(popularity, dict):
        return None, None, None
    model = popularity.get('render_model') or {}
    if not isinstance(model, dict):
        return None, None, None
    tree_set = model.get('tree_set') or {}
    if not isinstance(tree_set, dict):
        return None, None, None
    return (tree_set.get('class_name') or model.get('class_name'),
            tree_set.get('spec_name') or model.get('spec_name'),
            popularity.get('metadata_version_id'))


def _catalog(class_name, spec_name, version_id, *, ids=None):
    from botend.models import WowTalentNodeMetadata
    from botend.wow.talents.metadata import TalentMetadataProvider
    from botend.wow.talents.versioning import TalentVersionResolver
    if ids is None and version_id is None:
        version = TalentVersionResolver.get_default(TalentVersionResolver.USAGE_STATS)
        if version is None:
            return None, []
        version_id = version.pk
    if type(version_id) is not int or version_id <= 0:
        raise ValueError('天赋资料版本引用无效')
    rows = WowTalentNodeMetadata.all_objects.filter(
        talent_version_id=version_id, class_name=class_name, spec_name=spec_name,
        localization_only=False).select_related('talent_version').only(
            *ROW_FIELDS, 'talent_version__id', 'talent_version__key', 'talent_version__branch')
    if ids is not None:
        rows = rows.filter(pk__in=ids)
    rows = list(rows.order_by('pk'))
    if not rows:
        return version_id, []
    provider = TalentMetadataProvider(talent_version=rows[0].talent_version)
    provider._prime_display(rows)
    return version_id, [(row, provider._as_dict(row)) for row in rows]


def pack_metadata_references(detail, *, catalog_cache=None):
    class_name, spec_name, version_id = _scope(detail)
    if not isinstance(class_name, str) or not class_name or not isinstance(spec_name, str) or not spec_name:
        return detail, None
    # 不解释业务数据中已有的同名扩展，避免与内部引用发生冲突。
    def collides(value):
        if isinstance(value, dict):
            return REFERENCE in value or any(collides(child) for child in value.values())
        return isinstance(value, list) and any(collides(child) for child in value)
    if collides(detail):
        return detail, None
    cache_key = (class_name, spec_name, version_id)
    if catalog_cache is not None and cache_key in catalog_cache:
        version_id, catalog = catalog_cache[cache_key]
    else:
        version_id, catalog = _catalog(class_name, spec_name, version_id)
        if catalog_cache is not None:
            catalog_cache[cache_key] = version_id, catalog
    indexes = defaultdict(list)
    for row, data in catalog:
        for key in ('node_id', 'talent_id', 'spell_id', 'display_spell_id'):
            if data.get(key):
                indexes[key, data[key]].append((row, data))
    references, reference_ids = [], {}

    def visit(value):
        if isinstance(value, list):
            return [visit(child) for child in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(child) for key, child in value.items()}
        candidates = {}
        for key in ('node_id', 'talent_id', 'spell_id', 'display_spell_id'):
            if type(value.get(key)) is int:
                candidates.update((row.pk, (row, data)) for row, data in indexes.get((key, value[key]), ()))
        best = None
        for row, data in candidates.values():
            fields = [key for key in TEXT_FIELDS if isinstance(value.get(key), str)
                      and value[key] and value[key] == data.get(key)]
            score = sum(len(value[key].encode('utf-8')) for key in fields)
            if score > 24 and (best is None or score > best[0]):
                best = score, row, fields
        if best:
            _, row, fields = best
            if row.pk not in reference_ids:
                reference_ids[row.pk] = len(references)
                references.append([row.pk, row.node_id, row.talent_id, row.spell_id])
            result[REFERENCE] = [reference_ids[row.pk], sum(1 << TEXT_FIELDS.index(key) for key in fields)]
            for key in fields:
                del result[key]
        return result

    compact = visit(detail)
    if not references:
        return detail, None
    return compact, {'schema': 1, 'version_id': version_id, 'class_name': class_name,
                     'spec_name': spec_name, 'rows': references}


def unpack_metadata_references(detail, metadata):
    if (not isinstance(metadata, dict) or set(metadata) != {
            'schema', 'version_id', 'class_name', 'spec_name', 'rows'}
            or metadata['schema'] != 1 or type(metadata['version_id']) is not int
            or not isinstance(metadata['class_name'], str) or not isinstance(metadata['spec_name'], str)
            or not isinstance(metadata['rows'], list) or not 1 <= len(metadata['rows']) <= 4096):
        raise ValueError('天赋资料引用清单无效')
    refs = metadata['rows']
    if any(not isinstance(row, list) or len(row) != 4 or type(row[0]) is not int or row[0] <= 0
           or any(value is not None and type(value) is not int for value in row[1:]) for row in refs):
        raise ValueError('天赋资料节点引用无效')
    if len({row[0] for row in refs}) != len(refs):
        raise ValueError('天赋资料引用重复')
    _, catalog = _catalog(metadata['class_name'], metadata['spec_name'], metadata['version_id'],
                          ids=[row[0] for row in refs])
    by_id = {row.pk: (row, data) for row, data in catalog}
    resolved = []
    for pk, node_id, talent_id, spell_id in refs:
        found = by_id.get(pk)
        if found is None or [found[0].node_id, found[0].talent_id, found[0].spell_id] != [node_id, talent_id, spell_id]:
            raise ValueError('天赋资料引用缺失或节点身份已变化')
        resolved.append(found[1])

    def visit(value):
        if isinstance(value, list):
            return [visit(child) for child in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(child) for key, child in value.items() if key != REFERENCE}
        if REFERENCE in value:
            ref = value[REFERENCE]
            if (not isinstance(ref, list) or len(ref) != 2 or type(ref[0]) is not int
                    or not 0 <= ref[0] < len(resolved) or type(ref[1]) is not int
                    or not 0 < ref[1] < (1 << len(TEXT_FIELDS))):
                raise ValueError('天赋资料字段引用越界')
            for index, field in enumerate(TEXT_FIELDS):
                if ref[1] & (1 << index):
                    if field in result:
                        raise ValueError('天赋资料字段引用与本地值冲突')
                    result[field] = resolved[ref[0]][field]
        return result

    return visit(detail)
