"""按实际解析资料缓存人物天赋加工结果；批内共享元数据，批间重新核对。"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from botend.services.spec_stats_snapshot import content_digest

_batch = ContextVar('player_normalization_batch', default=None)


@contextmanager
def normalization_batch(season_id, class_name, spec_name):
    from botend.services.spec_aggregation_cache import load_work, save_work
    from botend.wow.talents.metadata import TalentMetadataProvider
    scope = f'player-normalization:{class_name}:{spec_name}'
    fingerprint = content_digest('player-normalization-v1')
    cached = load_work(season_id, scope, fingerprint)
    state = {'provider': TalentMetadataProvider(), 'old': cached if isinstance(cached, dict) else {},
             'used': {}, 'hits': 0, 'builds': 0}
    token = _batch.set(state)
    try:
        yield state
        save_work(season_id, scope, fingerprint, state['used'])
    finally:
        _batch.reset(token)


def normalized_view(talents, class_name, spec_name, build):
    state = _batch.get()
    if state is None:
        return build(talents, class_name=class_name, spec_name=spec_name)
    from botend.wow.talents.parser import normalize_talent_payload
    from botend.wow.talents.adapters import _should_merge_metadata
    provider = state['provider']
    payload = normalize_talent_payload(talents, class_name, spec_name)
    # 对实际有效资料取摘要，不能仅凭更新时间或导入代码宣称内容没变。
    resolved = [provider.merge_into_node(node, class_name, spec_name)
                if node.get('tree_type') != 'build_code' and
                _should_merge_metadata(node, class_name, spec_name) else node
                for node in payload['nodes']]
    key = content_digest([class_name, spec_name, talents, resolved])
    value = state['used'].get(key, state['old'].get(key))
    if value is None:
        value = build(talents, class_name=class_name, spec_name=spec_name, metadata_provider=provider)
        # 调用者只消费节点；不持久化重复树布局和整份渲染对象。
        value = {'nodes': value.get('nodes', [])}
        state['builds'] += 1
    else:
        state['hits'] += 1
    if len(state['used']) < 256:
        state['used'][key] = value
    return deepcopy(value)
