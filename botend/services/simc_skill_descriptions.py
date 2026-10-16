"""归一化表的技能、天赋和状态展示资料；不参与伤害计算。"""

import re
from functools import lru_cache

from django.db.models import Q

from botend.models import WowTalentNodeMetadata
from botend.services.wow_spell_display import SpellDisplayReader
from botend.wow.talents.versioning import TalentVersionResolver


def _key(value):
    return re.sub(r'[\s_-]+', '', str(value or '').casefold())


def _positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def attach_skill_damage_descriptions(actor, *, game_build='', branch='retail'):
    """按单个专精批量补齐说明，兼容旧快照的流式读取。原地修改展示副本。"""
    actions = [row for row in actor.get('actions', []) if isinstance(row, dict)]
    effects = [row for row in actor.get('global_skill_effects', []) if isinstance(row, dict)]
    variants = [row.get('variant') or {} for row in actions]
    conditions = [condition for row in variants + effects
                  for condition in row.get('runtime_conditions', []) if isinstance(condition, dict)]
    spell_ids = {sid for action in actions
                 for sid in (action.get('spell_id'), action.get('reporting_root_spell_id'))
                 if _positive_id(sid)}
    spell_ids.update(sid for effect in effects for sid in effect.get('source_spell_ids', [])
                     if _positive_id(sid))
    spell_ids.update(row['spell_id'] for row in conditions if _positive_id(row.get('spell_id')))
    talent_ids = {row['talent_id'] for row in variants + effects if _positive_id(row.get('talent_id'))}
    entries = {row['trait_entry_id'] for row in variants + effects
               if _positive_id(row.get('trait_entry_id'))}
    class_key = _key(actor.get('class'))
    spec_key = _key(actor.get('specialization') or actor.get('spec'))
    # 只查询当前专精引用的说明；不能把线上全量技能和历代天赋缓存到每个进程。
    try:
        version = TalentVersionResolver.resolve(version_key=branch)
    except ValueError:
        version = None
    talents = list(WowTalentNodeMetadata.objects.filter(talent_version=version).filter(
        Q(id__in=talent_ids) | Q(node_id__in=entries)
        | Q(spell_id__in=spell_ids) | Q(display_spell_id__in=spell_ids)
    ).values('id', 'node_id', 'spell_id', 'display_spell_id', 'class_name', 'spec_name')) if version else []
    talents = [row for row in talents if
               (not row['class_name'] or _key(row['class_name']) == class_key) and
               (not row['spec_name'] or _key(row['spec_name']) == spec_key)]
    spell_ids.update(sid for row in talents for sid in (row['spell_id'], row['display_spell_id']) if _positive_id(sid))
    reader = SpellDisplayReader(branch)
    reader.preload(spell_ids)
    def display(sid, node_id=None):
        return reader.get(sid, class_name=class_key, spec_name=spec_key, node_id=node_id)

    def talent_text(sid, locale, *, talent_id=None, entry_id=None):
        rows = [row for row in talents if
                (row['id'] == talent_id if talent_id else
                 row['node_id'] == entry_id if entry_id else
                 sid in (row['spell_id'], row['display_spell_id']))]
        field = 'description_zh' if locale == 'zhCN' else 'description'
        candidates = {display(row['display_spell_id'] or row['spell_id'], row['node_id']).get(field, '') for row in rows}
        candidates.discard('')
        return next(iter(candidates)) if len(candidates) == 1 else ''

    @lru_cache(maxsize=None)
    def spell_text(sid, locale, aura=False):
        row = display(sid)
        field = 'description_zh' if locale == 'zhCN' else 'description'
        return (row.get('aura_' + field) if aura else '') or row.get(field, '') or talent_text(sid, locale)

    def attach(row, ids=(), *, prefix='', aura=False, talent=False):
        for locale, suffix in (('zhCN', '_zh'), ('enUS', '')):
            field = f'{prefix}description{suffix}'
            text = ''
            if talent:
                text = talent_text(None, locale, talent_id=row.get('talent_id')) if row.get('talent_id') else ''
                if not text and row.get('trait_entry_id'):
                    text = talent_text(None, locale, entry_id=row['trait_entry_id'])
            if not text:
                text = next((value for sid in ids if _positive_id(sid)
                             if (value := spell_text(sid, locale, aura))), '')
            if text:
                row[field] = text
            # 已冻结的说明保留；没有资料时不生成无意义的提示文字。

    for action in actions:
        ids = [action.get('spell_id'), action.get('reporting_root_spell_id')]
        if action.get('reporting_root_component') and action.get('reporting_root_token') != action.get('token'):
            ids.reverse()
        attach(action, ids)
        variant = action.get('variant') or {}
        if variant.get('talent_id') or variant.get('trait_entry_id'):
            attach(variant, prefix='talent_', talent=True)
    for condition in conditions:
        # Reviewed global conditions may only carry token/ID, even when the
        # enclosing effect already has a Chinese title. Reuse the same bounded
        # spell lookup for old snapshots; never translate by token or copy the
        # enclosing talent's name to a different buff identity.
        name = str(display(condition.get('spell_id')).get('name_zh') or '').strip()
        if not re.search(r'[\u3400-\u9fff]', str(condition.get('name_zh') or '')):
            if re.search(r'[\u3400-\u9fff]', name):
                condition['name_zh'] = name
        if (condition.get('display_name')
                and not re.search(r'[\u3400-\u9fff]', str(condition['display_name']))
                and re.search(r'[\u3400-\u9fff]', str(condition.get('name_zh') or ''))):
            condition['display_name'] = condition['name_zh']
        attach(condition, [condition.get('spell_id')], aura=True)
    for effect in effects:
        aura = effect.get('source_kind') in {'buff', 'debuff'} or effect.get('source_type') == 'runtime_state'
        attach(effect, effect.get('source_spell_ids', []), aura=aura, talent=not aura)
        if not effect.get('description_zh') and effect.get('talent_description_zh'):
            effect['description_zh'] = effect['talent_description_zh']
        if not effect.get('description') and effect.get('talent_description'):
            effect['description'] = effect['talent_description']
    return actor
