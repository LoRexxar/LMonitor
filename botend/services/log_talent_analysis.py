"""只用战斗日志的选中条目生成天赋状态；角色当前配置不得参与。"""
from collections import defaultdict
from pathlib import Path

from botend.constants.hero_talents import (
    hero_subtree_name_by_id, hero_subtree_name_zh, spec_hero_subtree_ids,
)
from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.models import WowTalentNodeMetadata
from botend.wow.talents.build_code import TalentBuildCodeEncoder, _ImportBitWriter
from botend.wow.talents.metadata import AUTHORITATIVE_TALENT_SOURCES, TalentMetadataProvider
from botend.wow.talents.versioning import TalentVersionResolver


def _integer(value):
    try:
        number = int(value)
        return number if not isinstance(value, bool) and float(value) == number else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def prepare_log_talents(records, class_name, spec_name, *, provider=None, metadata=None, encode_builds=True):
    """完整保留日志队列，失败只清空天赋投影，不影响装备、DPS 等样本。"""
    records = list(records)
    if not records:
        return []
    provider = provider or TalentMetadataProvider(usage=TalentVersionResolver.USAGE_STATS)
    version = provider.resolved_version
    if metadata is None:
        rows = WowTalentNodeMetadata.objects.filter(
            talent_version=version, class_name=class_name, spec_name=spec_name,
            source__in=AUTHORITATIVE_TALENT_SOURCES,
        )
        metadata = [provider._as_dict(row) for row in provider._prime_display(rows)]
    entries = {_integer(node.get('node_id')): node for node in metadata}
    by_spell = defaultdict(list)
    entries_by_trait = defaultdict(list)
    for node in metadata:
        entries_by_trait[node['talent_id']].append(node)
        for spell in {node.get('spell_id'), node.get('display_spell_id')} - {None, 0}:
            by_spell[_integer(spell)].append(node)
    decoder_nodes = list(provider.get_decoder_node_list(class_name) or [])
    decoder_by_trait = {_integer(node.get('talent_id')): node for node in decoder_nodes}
    allowed_heroes = spec_hero_subtree_ids(class_name, spec_name)
    spec_id = next((key for key, identity in SPEC_IDENTITY_MAP.items()
                    if identity == (class_name, spec_name)), 0)
    grants = getattr(version, 'granted_entries_json', {}) or {}
    grant_ids = set(grants.get('specs', {}).get(str(spec_id), ())) if (
        grants.get('build') and grants['build'] == getattr(version, 'current_build', None)
    ) else set()
    source_dir = Path(getattr(version, 'source_dir', '') or '.')
    # 没有完整节点序列时仍能比较日志条目，但不能生成可能错位的导入码。
    can_encode = bool(encode_builds and spec_id and decoder_nodes and
                      (source_dir / 'TraitNode.csv').is_file() and
                      (source_dir / 'TraitNodeXTraitNodeEntry.csv').is_file())
    writer = _ImportBitWriter()
    writer.write(2, 8)
    writer.write(spec_id, 16)
    writer.write(0, 128)
    reference = writer.to_string()
    resolved_cache, code_cache = {}, {}
    result = []
    for original in records:
        row = dict(original, talent_build_code='')
        raw_nodes = original.get('talents_json') or []
        row['_log_talent_status'] = 'missing' if not raw_nodes else 'ok'
        selected = {}
        for raw in raw_nodes if isinstance(raw_nodes, list) else [None]:
            if not isinstance(raw, dict) or raw.get('invalid'):
                row['_log_talent_status'] = 'unresolved'
                break
            raw_points = raw.get('points', raw.get('rank', 0))
            try:
                points = int(raw_points)
                if isinstance(raw_points, bool) or float(raw_points) != points:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                row['_log_talent_status'] = 'unresolved'
                break
            if points < 0:
                row['_log_talent_status'] = 'conflict'
                break
            if points == 0:
                continue
            # WCL talentID 和 CombatantInfo.id 是 Entry ID，不是 Spell ID。
            entry_id = _integer(raw.get('entry_id') or raw.get('id') or
                                raw.get('node_id') or raw.get('talentID') or raw.get('talent_id'))
            spell_id = _integer(raw.get('spellID') or raw.get('spell_id'))
            cache_key = (entry_id, spell_id)
            if cache_key not in resolved_cache:
                node = entries.get(entry_id)
                if not node and spell_id and spell_id != entry_id:
                    candidates = by_spell.get(spell_id, [])
                    node = candidates[0] if len(candidates) == 1 else None
                resolved_cache[cache_key] = node
            node = resolved_cache[cache_key]
            if not node:
                row['_log_talent_status'] = 'unresolved'
                break
            tree = node.get('tree_type')
            if _integer(node.get('talent_id')) <= 0 or tree not in {'class', 'spec', 'hero', 'hero_anchor'}:
                row['_log_talent_status'] = 'unresolved'
                break
            subtree = _integer(node.get('db2_subtree_id'))
            if tree == 'hero' and subtree not in allowed_heroes:
                row['_log_talent_status'] = 'conflict'
                break
            if points > _integer(node.get('max_points') or 1):
                row['_log_talent_status'] = 'conflict'
                break
            payload = dict(node, points=points, selected=True, source='wcl_log', _log_resolved=True,
                           purchased=node['node_id'] not in grant_ids)
            previous = selected.get(node['node_id'])
            if previous and previous['points'] != points:
                row['_log_talent_status'] = 'conflict'
                break
            selected[node['node_id']] = payload
        nodes, selected_for_code = {}, []
        exportable = True
        by_trait = defaultdict(list)
        for node in selected.values():
            by_trait[node['talent_id']].append(node)
        for trait_id, options in by_trait.items():
            decoder = decoder_by_trait.get(trait_id) or {}
            all_options = entries_by_trait[trait_id]
            is_apex = bool(decoder.get('is_apex_talent') or
                           TalentMetadataProvider._is_apex_entry_group(options[0], all_options))
            if len(options) > 1 and not is_apex:
                row['_log_talent_status'] = 'conflict'
                break
            node = dict(options[0])
            node['points'] = sum(option['points'] for option in options) if is_apex else node['points']
            if is_apex:
                node.update(max_points=decoder.get('max_points') or
                            sum(option.get('max_points') or 1 for option in all_options),
                            is_apex_talent=True)
            choices = decoder.get('choice_options') or []
            if not choices and len(all_options) > 1 and not is_apex:
                # 能识别选中条目就能比较；缺少位序时不能冒充可导入的构筑。
                choices = all_options
                exportable = False
            choice_index = next((i for i, choice in enumerate(choices)
                                 if choice.get('node_id') == node['node_id']), None)
            if choices and not is_apex and choice_index is None:
                row['_log_talent_status'] = 'unresolved'
                break
            node.update(is_choice_node=bool(choices and not is_apex),
                        choice_selection=choice_index if not is_apex else None)
            selected_for_code.append(node)
            if node['tree_type'] == 'hero_anchor' and not is_apex:
                continue
            tree = 'spec' if is_apex else node['tree_type']
            key = f'{tree}:{trait_id}'
            name = node.get('name_zh') or node.get('name') or f'天赋 {node["node_id"]}'
            nodes[key] = dict(
                node, node_key=key, tree_type=tree, name=name,
                tree_label={'class': '职业天赋', 'spec': '专精天赋', 'hero': '英雄天赋'}.get(tree, '天赋'),
                choice_key=node['node_id'] if node['is_choice_node'] else None,
                choice_name=name if node['is_choice_node'] else '',
            )
        hero_ids = {node['db2_subtree_id'] for node in nodes.values() if node['tree_type'] == 'hero'}
        if len(hero_ids) > 1:
            row['_log_talent_status'] = 'conflict'
        trees = {node['tree_type'] for node in nodes.values()}
        if row['_log_talent_status'] == 'ok' and not {'class', 'spec', 'hero'}.issubset(trees):
            row['_log_talent_status'] = 'partial'
        if row['_log_talent_status'] != 'ok':
            row['talents_json'] = []
            result.append(row)
            continue
        signature = tuple(sorted((key, node['points'], node.get('choice_key') or -1)
                                 for key, node in nodes.items()))
        selectors = tuple(sorted((node['talent_id'], node['node_id'])
                                 for node in selected_for_code
                                 if node['tree_type'] == 'hero_anchor' and not node.get('is_apex_talent')))
        if any(TalentBuildCodeEncoder._is_hidden_hero_selector(node) for node in decoder_nodes) and not selectors:
            exportable = False
        code_key = (signature, selectors, exportable)
        if code_key not in code_cache:
            code_cache[code_key] = ''
            if can_encode and exportable:
                lookup = TalentBuildCodeEncoder._build_selected_lookup(selected_for_code, decoder_nodes)
                all_mapped = (len(lookup) == len(selected_for_code)
                              and sum(state['points'] for state in lookup.values()) ==
                              sum(node['points'] for node in selected_for_code))
                if all_mapped:
                    code_cache[code_key] = TalentBuildCodeEncoder.encode_node_states(
                        reference, decoder_nodes, selected_for_code)
        row['talent_build_code'] = code_cache[code_key]
        row['talents_json'] = list(nodes.values())
        row['_log_talent_state'] = {
            'keys': set(nodes), 'nodes': nodes,
            'hero_talent_summary': [
                {'subtree_id': key, 'name': hero_subtree_name_zh(hero_subtree_name_by_id(key)),
                 'selected_count': sum(node.get('db2_subtree_id') == key for node in nodes.values())}
                for key in sorted(hero_ids)
            ],
        }
        result.append(row)
    return result


def add_template_analysis(popularity, states_by_key, records):
    """汇总核心方案和相对完整模板的节点变化；不截断长尾样本。"""
    from collections import Counter
    statuses = Counter(row.get('_log_talent_status', 'ok') for row in records)
    popularity.update(
        analysis_version=1, source='wcl-log', sample_total=len(records),
        excluded_count=len(records) - popularity['total'],
        missing_count=statuses['missing'], unresolved_count=statuses['unresolved'],
        conflict_count=statuses['conflict'], partial_count=statuses['partial'],
    )
    builds = popularity['builds']
    if not builds:
        return popularity
    # 原始日志顺序不能决定全局模板；并列按稳定状态键排序。
    builds.sort(key=lambda build: (-build['count'], build.get('state_key', '')))
    template = builds[0]
    popularity['primary_template'] = template
    popularity['template_code'] = template['code']
    popularity['template_count'] = template['count']
    families = {}
    for build in builds:
        nodes = states_by_key[build['state_key']]['nodes']
        core = tuple(sorted((key, node['points'], node.get('choice_key') or -1)
                            for key, node in nodes.items() if node['tree_type'] != 'class'))
        family = families.setdefault(core, {'count': 0, 'builds': [], 'representative': build})
        family['count'] += build['count']
        family['builds'].append(build)
    ordered = sorted(families.values(), key=lambda family: (-family['count'], family['representative']['state_key']))
    for index, family in enumerate(ordered, 1):
        family.update(rank=index, pct=round(family['count'] / popularity['total'] * 100, 1),
                      variant_count=len(family['builds']),
                      is_template_family=template in family['builds'])
    popularity['families'] = ordered
    popularity['build_count'] = len(builds)
    popularity['family_count'] = len(ordered)
    popularity['remaining_family_count'] = max(0, len(ordered) - 3)
    popularity['primary_template']['overall_pct'] = round(template['count'] / popularity['total'] * 100, 1)
    popularity['template_family'] = next(f for f in ordered if f['is_template_family'])
    baseline = states_by_key[template['state_key']]['nodes']
    changes = {}
    for index, build in enumerate(builds, 1):
        nodes = states_by_key[build['state_key']]['nodes']
        diffs = []
        for key in sorted(set(nodes) | set(baseline)):
            before, after = baseline.get(key), nodes.get(key)
            state = lambda node: (node['points'], node.get('choice_key')) if node else (0, None)
            if state(before) == state(after):
                continue
            node = after or before
            kind = 'added' if not before else 'missing' if not after else 'changed'
            change_key = (key, state(before), state(after))
            diff = dict({field: node.get(field) for field in ('node_key', 'tree_type', 'tree_label')},
                        kind=kind, before_name=before['name'] if before else '不点',
                        after_name=after['name'] if after else '不点',
                        before_icon=(before.get('icon') or '') if before else '',
                        after_icon=(after.get('icon') or '') if after else '',
                        before_points=before['points'] if before else 0,
                        after_points=after['points'] if after else 0)
            item = changes.setdefault(change_key, dict(diff, count=0))
            item['count'] += build['count']
            diffs.append(diff)
        build['template_diffs'] = diffs
        build['rank'] = index
        build['is_template'] = build['state_key'] == template['state_key']
        build['diff_count'] = len(diffs)
        build['overall_pct'] = round(build['count'] / popularity['total'] * 100, 1)
        # 新页面使用统一的主模板差异，不重复保存旧英雄分组的整份节点资料。
        for field in ('added_talents', 'missing_talents', 'changed_talents'):
            build.pop(field, None)
    popularity['common_changes'] = sorted(
        [dict(item, pct=round(item['count'] / popularity['total'] * 100, 1))
         for item in changes.values()], key=lambda item: (-item['count'], item['node_key'], item['after_name']))
    for group in popularity.get('hero_groups', []):
        group['builds'] = [{key: build[key] for key in ('code', 'count', 'pct', 'state_key')}
                           for build in group['builds']]
    return popularity
