"""聚合文件按天赋 ID 补充当前分支文本，保留样本与使用率。"""
from botend.models import WowTalentNodeMetadata
from botend.wow.talents.versioning import TalentVersionResolver


def refresh_aggregate_talents(payload, *, class_name='', spec_name='', branch='retail'):
    rows = []
    def visit(value, inside=False):
        if isinstance(value, dict):
            if inside and value.get('node_id'):
                rows.append(value)
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    visit(child, inside or key.startswith('talent'))
        elif isinstance(value, list):
            for child in value:
                visit(child, inside)
    visit(payload)
    if not rows:
        return payload
    try:
        version = TalentVersionResolver.resolve(version_key=branch)
    except ValueError:
        return payload
    ids = {row['node_id'] for row in rows if isinstance(row['node_id'], int) or str(row['node_id']).isdigit()}
    metadata = WowTalentNodeMetadata.objects.filter(talent_version=version, node_id__in=ids)
    if class_name:
        metadata = metadata.filter(class_name=class_name)
    if spec_name:
        metadata = metadata.filter(spec_name=spec_name)
    lookup = {(str(row.node_id), row.tree_type): row for row in metadata}
    for row in rows:
        current = lookup.get((str(row['node_id']), row.get('tree_type') or 'spec'))
        if current:
            for field, value in (('name', current.name_zh or current.name), ('name_zh', current.name_zh),
                                 ('description', current.description_zh or current.description),
                                 ('description_zh', current.description_zh), ('icon', current.icon)):
                if value:
                    row[field] = value
    return payload
