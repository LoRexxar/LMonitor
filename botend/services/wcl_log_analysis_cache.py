"""正式日志按录像／战斗／角色身份复用，检查点只保留当前样本。"""
from copy import deepcopy
from pathlib import Path
import json
import time

from botend.models import WowTalentNodeMetadata
from botend.services import wcl_combatant_snapshot as combatants
from botend.services.log_talent_analysis import prepare_log_talents
from botend.services.spec_aggregation_cache import load_work, save_work
from botend.services.spec_stats_snapshot import content_digest
from botend.wow.talents.metadata import AUTHORITATIVE_TALENT_SOURCES, TalentMetadataProvider
from botend.wow.talents.versioning import TalentVersionResolver


# 日志富化或天赋解析语义改变时升级，排名排序规则不属于此版本。
SCHEMA = 'wcl-log-analysis-v2-identity'
LOG_FIELDS = ('gear_json', 'talents_json', 'stats_json', 'race', 'wcl_actor_id')


def observation_key(row, event):
    """正式日志只看录像／战斗／角色身份；旧格式或缺失字段保留兼容校验。

    只有存储层确认不可覆盖的事实可以使用身份键。排名中的后备字段仍会
    更新，不能把未取得的日志或未使用正式事实的字段误当成不可变数据。
    """
    identity = getattr(event, 'cache_identity', None)
    if identity is not None:
        fallback = {}
        if not event.get('talentTree'):
            fallback['talents'] = row.get('talents_json')
        if not any(isinstance(item, dict) and combatants._positive_int(item.get('id'))
                   for item in event.get('gear') or []):
            fallback['gear'] = row.get('gear_json')
        return content_digest(['stored', identity, fallback])
    # 旧表与内存输入没有不可变存储保证；不能只凭名称缓存后备数据。
    return content_digest(['legacy', combatants._fight_key(row),
        combatants._realm(row.get('realm')), str(row.get('character_name') or '').strip().casefold(),
        event, row.get('gear_json'), row.get('talents_json')])


def scope_fingerprint(records, facts, class_name, spec_name, *, scope, metadata):
    """现有工作缓存头保存日志集合与排名两部分，避免另存重复统计正文。"""
    keys = [observation_key(row, combatants.match_combatant(
        facts.get(combatants._fight_key(row), []), row, class_name, spec_name)) for row in records]
    logs = content_digest([SCHEMA, scope, class_name, spec_name, sorted(keys), metadata])
    top_ids = {id(row) for row in sorted(records, key=lambda r: r.get('dps') or 0, reverse=True)[:5]}
    # 正文只影响日志统计及Top5展示，其他排名行不重复哈希装备／天赋。
    ranking = content_digest([{k: v for k, v in row.items() if k != 'last_updated'
        and (k not in ('gear_json', 'talents_json') or id(row) in top_ids)} for row in records])
    return f'log2:{logs}:{ranking}'


def reusable_log_statistics(season_id, scope, fingerprint):
    """排名变了、日志集合未变时，取出旧范围中可复用的纯日志统计。"""
    from .spec_aggregation_cache import work_fingerprint_hint
    previous = work_fingerprint_hint(season_id, scope)
    if (isinstance(previous, str) and fingerprint.startswith('log2:')
            and previous.rsplit(':', 1)[0] == fingerprint.rsplit(':', 1)[0]):
        return load_work(season_id, scope, previous)
    return None


def _metadata(provider, class_name, spec_name):
    version = provider.resolved_version
    rows = WowTalentNodeMetadata.objects.filter(talent_version=version, class_name=class_name,
        spec_name=spec_name, source__in=AUTHORITATIVE_TALENT_SOURCES)
    metadata = [provider._as_dict(row) for row in provider._prime_display(rows)]
    source = Path(getattr(version, 'source_dir', '') or '.')
    generation = content_digest([SCHEMA, class_name, spec_name, metadata,
        provider.get_decoder_node_list(class_name), getattr(version, 'pk', None),
        getattr(version, 'branch', None), getattr(version, 'current_build', None),
        getattr(version, 'granted_entries_json', {}) or {},
        [(source / name).is_file() for name in ('TraitNode.csv', 'TraitNodeXTraitNodeEntry.csv')]])
    return metadata, generation


def _copy_plan(value):
    """节点池只检查一次嵌套字段，展开样本时仅复制可变容器。"""
    if isinstance(value, dict):
        return value, {key: _copy_plan(child) for key, child in value.items() if isinstance(child, (dict, list))}
    if isinstance(value, list):
        return value, {i: _copy_plan(child) for i, child in enumerate(value) if isinstance(child, (dict, list))}
    return value, None


def _copy_value(plan):
    value, children = plan
    if children is None:
        return value
    result = value.copy()
    for key, child in children.items():
        result[key] = _copy_value(child)
    return result


def _unpack(entry, plans):
    """恢复该日志的独立对象，避免统计或调用方修改污染其他样本。"""
    indexes, status, code, heroes = entry['talent']
    if (not isinstance(indexes, list) or status not in {'ok', 'missing', 'partial', 'conflict', 'unresolved'}
            or not isinstance(code, str) or not isinstance(heroes, list)
            or any(type(i) is not int or i < 0 or i >= len(plans) for i in indexes)):
        raise ValueError('日志解析检查点格式无效')
    nodes = [_copy_value(plans[index]) for index in indexes]
    result = {'talents_json': nodes, 'talent_build_code': code, '_log_talent_status': status}
    if status == 'ok':
        by_key = {node['node_key']: node for node in nodes}
        result['_log_talent_state'] = {'keys': set(by_key), 'nodes': by_key,
                                      'hero_talent_summary': deepcopy(heroes)}
    return result


def _unpack_log(value, plans):
    result = dict(value)
    for field in ('gear_json', 'talents_json'):
        if isinstance(result.get(field), list):
            indexes = result[field]
            if any(type(i) is not int or i < 0 or i >= len(plans) for i in indexes):
                raise ValueError('日志内容引用无效')
            result[field] = [_copy_value(plans[i]) for i in indexes]
    return result


def prepare_cached_logs(records, class_name, spec_name, *, season_id, scope, facts, timings=None):
    """只解析新增/变化日志；返回旧口径富化记录、已解析记录及本次元数据提供器。

    同一范围最多一个检查点，退出榜单的样本在下次写入时移除。中央事实仍是
    唯一数据来源；文件损坏、元数据变更只触发本地重建，绝不在此请求上游。
    """
    records = list(records)
    slot = f'log-analysis:{scope}'
    if not records:
        previous = load_work(season_id, slot, SCHEMA)
        if isinstance(previous, dict) and previous.get('entries'):
            save_work(season_id, slot, SCHEMA, {'layout': 2, 'generation': '', 'entries': {}, 'nodes': []},
                      raw_json=True, durable=False)
        return [], [], None
    started = time.monotonic()
    provider = TalentMetadataProvider(usage=TalentVersionResolver.USAGE_STATS)
    metadata, generation = _metadata(provider, class_name, spec_name)
    previous = load_work(season_id, slot, SCHEMA)
    previous = previous if isinstance(previous, dict) and previous.get('layout') == 2 else {}
    entries, pool = previous.get('entries', {}), previous.get('nodes', [])
    if not isinstance(entries, dict) or not isinstance(pool, list):
        entries, pool = {}, []
    plans = [_copy_plan(value) for value in pool]
    same_metadata = previous.get('generation') == generation
    events = [combatants.match_combatant(facts.get(combatants._fight_key(row), []),
              row, class_name, spec_name) for row in records]
    catalog = combatants._enchant_catalog([
        {'gear': (event or {}).get('gear') or row.get('gear_json') or []}
        for row, event in zip(records, events)])
    keys, logs, talents, missing = [], {}, {}, {}
    loaded_logs, loaded_talents = set(), set()
    reused = 0
    for row, event in zip(records, events):
        gear = (event or {}).get('gear') or row.get('gear_json') or []
        enchant_ids = {combatants._positive_int(item.get(field))
            for item in gear if isinstance(item, dict)
            for field in ('permanentEnchant', 'temporaryEnchant')}
        # 排名主键、DPS、刷新时间和出现顺序不参与日志身份或内容。
        key = content_digest([observation_key(row, event), class_name, spec_name,
            [(eid, catalog.get(eid)) for eid in sorted(e for e in enchant_ids if e)]])
        keys.append(key)
        if key in logs or key in missing:
            continue
        entry = entries.get(key)
        if isinstance(entry, dict) and isinstance(entry.get('log'), dict):
            try:
                logs[key] = _unpack_log(entry['log'], plans)
                loaded_logs.add(key)
            except (KeyError, ValueError, TypeError, IndexError):
                missing[key] = row
                continue
            if same_metadata:
                try:
                    talents[key] = _unpack(entry, plans)
                    loaded_talents.add(key)
                    reused += 1
                except (KeyError, ValueError, TypeError, IndexError):
                    pass
        else:
            missing[key] = row
    if missing:
        enriched = combatants.enrich_dungeon_records(list(missing.values()), class_name, spec_name, facts=facts)
        for key, row in zip(missing, enriched):
            logs[key] = {field: row[field] for field in LOG_FIELDS if field in row}
    unparsed = [key for key in logs if key not in talents]
    if unparsed:
        parsed = prepare_log_talents([logs[key] for key in unparsed], class_name, spec_name,
                                    provider=provider, metadata=metadata)
        for key, row in zip(unparsed, parsed):
            talents[key] = {field: row[field] for field in
                ('talents_json', 'talent_build_code', '_log_talent_status', '_log_talent_state') if field in row}
    # 节点元数据按内容去重，100人不保存100份相同说明文字。
    if unparsed or set(entries) != set(keys) or not same_metadata:
        node_ids, nodes, current, old_indexes = {}, [], {}, {}
        def intern(value):
            # 仅作本次写入的相等比较；文件完整性仍由工作缓存的摘要验证。
            identity = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if identity not in node_ids:
                node_ids[identity] = len(nodes)
                nodes.append(value)
            return node_ids[identity]
        def remap(index):
            if index not in old_indexes:
                old_indexes[index] = intern(pool[index])
            return old_indexes[index]
        for key in dict.fromkeys(keys):
            parsed = talents[key]
            indexes = ([remap(index) for index in entries[key]['talent'][0]] if key in loaded_talents
                       else [intern(node) for node in parsed['talents_json']])
            log = dict(entries[key]['log'] if key in loaded_logs else logs[key])
            for field in ('gear_json', 'talents_json'):
                if isinstance(log.get(field), list):
                    convert = remap if key in loaded_logs else intern
                    log[field] = [convert(value) for value in log[field]]
            current[key] = {'log': log, 'talent': [indexes,
                parsed['_log_talent_status'], parsed['talent_build_code'],
                (parsed.get('_log_talent_state') or {}).get('hero_talent_summary', [])]}
        save_work(season_id, slot, SCHEMA, {'layout': 2, 'generation': generation,
            'entries': current, 'nodes': nodes}, raw_json=True, durable=False)
    enriched, prepared, used = [], [], set()
    for original, key in zip(records, keys):
        row = dict(original)
        row.pop('wcl_actor_id', None)
        row.pop('_log_talent_state', None)
        row.update(deepcopy(logs[key]) if key in used else logs[key])
        enriched.append(row)
        prepared.append({**row, **(deepcopy(talents[key]) if key in used else talents[key])})
        used.add(key)
    if timings is not None:
        timings['log_analysis'] += time.monotonic() - started
        timings['log_analysis_reused'] += reused
        timings['log_analysis_parsed'] += len(unparsed)
    return enriched, prepared, provider
