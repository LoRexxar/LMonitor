"""聚合只依赖当前结构；身份变化使旧计算失效，不保存历史身份。"""
from contextlib import contextmanager

from django.db import transaction

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.wow.talents.versioning import TalentVersionResolver


def effective_grants(version, class_name):
    """只纳入该职业当前构建有效的赠送节点，构建标签本身不触发重算。"""
    from botend.constants.wow import SPEC_IDENTITY_MAP
    snapshot = getattr(version, 'granted_entries_json', {}) or {}
    if not snapshot.get('build') or snapshot['build'] != getattr(version, 'current_build', None):
        return {}
    ids = {str(key) for key, identity in SPEC_IDENTITY_MAP.items() if identity[0] == class_name}
    return {key: value for key, value in snapshot.get('specs', {}).items() if key in ids}


def structure_token(class_name, *, version=None, lock=False):
    """导入码解码器使用同职业的节点全集，因此共享树变化影响该职业各专精。"""
    from .spec_stats_snapshot import content_digest
    version = version or TalentVersionResolver.get_default(TalentVersionResolver.USAGE_STATS)
    rows, dumps = [], []
    if version:
        fields = ('spec_name', 'tree_type', 'node_id', 'talent_id', 'spell_id',
                  'display_spell_id', 'row', 'column', 'max_points', 'parents_json',
                  'db2_subtree_id', 'db2_tree_id', 'db2_component_id', 'flags', 'source')
        query = WowTalentNodeMetadata.objects.filter(talent_version=version, class_name=class_name)
        if lock:
            query = query.select_for_update()
        rows = list(query.order_by(*fields[:5], 'pk').values(*fields))
        from pathlib import Path
        from botend.wow.spell_text import _file_cache_key
        from .dungeon_aggregation import _structural_dump_digest
        entries = tuple(sorted({row['node_id'] for row in rows if row['node_id']}))
        trees = tuple(sorted({row['db2_tree_id'] for row in rows if row['db2_tree_id']}))
        dumps = [_structural_dump_digest(_file_cache_key(Path(version.source_dir or '') / name), entries, trees)
                 for name in ('TraitNodeXTraitNodeEntry.csv', 'TraitNode.csv')]
    return content_digest({'version': getattr(version, 'pk', None), 'rows': rows,
                           'dumps': dumps, 'grants': effective_grants(version, class_name)})


@contextmanager
def publication_metadata_guard(class_name, expected):
    """仅最终落盘期间锁当前版本，与中央发布串行；计算期间变化则重试。"""
    if expected is None:
        yield
        return
    with transaction.atomic():
        # 先做锁定读，再解析当前版本，避免可重复读事务使用等待锁之前的旧快照。
        versions = list(WowTalentVersion.objects.select_for_update().filter(branch='retail').order_by('pk'))
        current = [row for row in versions if row.is_active and row.status == 'active']
        version = next((row for row in current if row.key == 'retail'), None)
        if version is None and current:
            version = max(current, key=lambda row: (str(row.activated_at or ''), row.pk))
        if structure_token(class_name, version=version, lock=True) != expected:
            raise ValueError('聚合期间天赋结构已更新，保留原入口并重试')
        yield
