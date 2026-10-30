"""技能行天赋关联：只接受当前分量的实际倍率来源或当前施法根的解锁证据。"""
import copy
import math


def _positive(value):
    return type(value) is int and value > 0


def collect_skill_talent_links(action, *, component_name=None):
    variant = action.get('variant') or {}
    reference = variant.get('reference_context') or {}
    context = variant.get('activation_context') or {}
    traits = {t['trait_entry_id']: t for t in [*(reference.get('traits') or []), *(context.get('traits') or [])]
              if isinstance(t, dict) and _positive(t.get('trait_entry_id'))}
    selected = set(reference.get('trait_entry_ids') or []) | set(context.get('trait_entry_ids') or [])
    links = {}

    def add(entry, evidence):
        if entry not in selected or entry not in traits or entry == variant.get('trait_entry_id'):
            return
        row = links.setdefault(entry, {k: copy.deepcopy(traits[entry][k])
                                      for k in ('trait_entry_id', 'name', 'name_zh') if k in traits[entry]})
        if evidence not in row.setdefault('evidence', []):
            row['evidence'].append(evidence)

    skill = action.get('spell_id')
    if _positive(skill):
        sources = {(s.get('source_spell_id'), s.get('effect_index'))
                   for kind in ((component_name,) if component_name else ('direct', 'tick'))
                   for s in ((action.get('baseline') or {}).get(kind) or {}).get('runtime_factor_sources', [])
                   if s.get('evidence') == 'native_multiplier_dataflow_v1'
                   and _positive(s.get('source_spell_id')) and _positive(s.get('effect_index'))
                   and type(s.get('factor')) in (int, float) and math.isfinite(s['factor'])
                   and not math.isclose(s['factor'], 1.0, rel_tol=0, abs_tol=1e-12)}
        for fact in action.get('selected_trait_effects') or []:
            index = fact.get('effect_index')
            if type(index) is not int or index < 0:
                continue
            source = (fact.get('source_spell_id'), index + 1)
            if source in sources:
                add(fact.get('trait_entry_id'), {'kind': 'native_modifier', 'skill_spell_id': skill,
                                               'source_spell_id': source[0], 'effect_index': source[1]})
    root = action.get('reporting_root_spell_id') or skill
    if _positive(root) and root in (context.get('action_spell_ids') or []):
        for fact in context.get('evidence') or []:
            if fact.get('replacement_spell_id') == root:
                add(fact.get('trait_entry_id'), {'kind': 'native_action_unlock', 'skill_spell_id': root})
    return [links[k] for k in sorted(links)]


def merge_skill_talent_links(target, incoming):
    for row in incoming:
        existing = next((r for r in target if r['trait_entry_id'] == row['trait_entry_id']), None)
        if existing is None:
            target.append(copy.deepcopy(row))
        else:
            for evidence in row['evidence']:
                if evidence not in existing['evidence']:
                    existing['evidence'].append(copy.deepcopy(evidence))


def apply_skill_talent_links(action, links):
    """完整配置仅留在原始探针；展示响应中不保留未证明的前置名单和旧说明。"""
    variant = action.get('variant')
    if not isinstance(variant, dict):
        return
    variant.pop('reference_context', None)
    variant.pop('activation_context', None)
    variant.pop('related_talents', None)
    if links:
        variant['related_talents'] = sorted(copy.deepcopy(links), key=lambda r: r['trait_entry_id'])
