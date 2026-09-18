"""归一化表的技能、天赋和状态说明；只补展示字段，不参与伤害计算。"""

import re
import sys
from functools import lru_cache

from django.db import connection

from botend.constants.wow import SPEC_ACTIVE_AURA_IDS, SPEC_CONDITION_INDEX
from botend.models import WowSpellSnapshot, WowTalentNodeMetadata
from botend.wow.spell_text import SpellTextResolver


def _key(value):
    return re.sub(r'[\s_-]+', '', str(value or '').casefold())


def _positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@lru_cache(maxsize=4)
def _database_catalog(connection_key, build):
    """一次读取说明目录，避免流式返回旧快照时按 actor 重复查询数据库。"""
    spells = list(WowSpellSnapshot.objects.filter(
        branch='wow', locale__in=['zhCN', 'enUS'],
    ).values('spell_id', 'locale', 'description', 'aura_description', 'snapshot_build'))
    talents = list(WowTalentNodeMetadata.all_objects.values(
        'id', 'node_id', 'spell_id', 'display_spell_id', 'class_name', 'spec_name',
             'description', 'description_zh', 'talent_version__current_build'))
    return spells, talents


def _current_database_catalog(build):
    connection.ensure_connection()
    # 测试套件会在同一进程内重建数据库，不能复用上一个测试的数据目录。
    if any(str(argument).lower() == 'test' for argument in sys.argv):
        return _database_catalog.__wrapped__(id(connection.connection), build)
    return _database_catalog(id(connection.connection), build)


def attach_skill_damage_descriptions(actor, *, game_build=''):
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
    database_spells, database_talents = _current_database_catalog(game_build)
    talents = [row for row in database_talents if (
        row['id'] in talent_ids or row['node_id'] in entries
        or row['spell_id'] in spell_ids or row['display_spell_id'] in spell_ids
    )] if spell_ids or talent_ids else []
    # 明确的职业、专精边界必须匹配；同名天赋不能跨专精借用说明。
    talents = [row for row in talents if
               (not row['class_name'] or _key(row['class_name']) == class_key) and
               (not row['spec_name'] or _key(row['spec_name']) == spec_key)]
    for row in talents:
        spell_ids.update(sid for sid in (row['spell_id'], row['display_spell_id']) if _positive_id(sid))
    spells = {(row['spell_id'], row['locale']): row for row in database_spells
              if row['spell_id'] in spell_ids} if spell_ids else {}
    resolvers = {locale: SpellTextResolver(locale=locale, snapshot_build=game_build)
                 for locale in ('zhCN', 'enUS')}
    spec_index = next((value for (cls, spec), value in SPEC_CONDITION_INDEX.items()
                       if _key(cls) == class_key and _key(spec) == spec_key), None)
    active_auras = next((value for (cls, spec), value in SPEC_ACTIVE_AURA_IDS.items()
                         if _key(cls) == class_key and _key(spec) == spec_key), set())

    @lru_cache(maxsize=None)
    def resolved(text, sid, locale):
        return resolvers[locale].resolve(text, sid, spec_index=spec_index, active_aura_ids=active_auras)

    def talent_text(sid, locale, *, talent_id=None, entry_id=None):
        rows = [row for row in talents if
                (row['id'] == talent_id if talent_id else
                 row['node_id'] == entry_id if entry_id else
                 sid in (row['spell_id'], row['display_spell_id']))]
        field = 'description_zh' if locale == 'zhCN' else 'description'
        rows = [row for row in rows if row[field].strip()]
        if not rows:
            return ''
        def rank(row):
            return (bool(game_build and row['talent_version__current_build'] == game_build),
                    bool(row['spec_name']), bool(row['class_name']))
        best = max(map(rank, rows))
        candidates = {(row[field].strip(), row['display_spell_id'] or row['spell_id'])
                      for row in rows if rank(row) == best}
        # 同优先级的说明相互冲突时保留空值，不能随数据库顺序选一条。
        if len({text for text, _ in candidates}) != 1:
            return ''
        text, text_sid = next(iter(candidates))
        return resolved(text, text_sid, locale)

    @lru_cache(maxsize=None)
    def spell_text(sid, locale, aura=False):
        row = spells.get((sid, locale), {})
        fields = ('aura_description', 'description') if aura else ('description', 'aura_description')
        text = next((row.get(field, '').strip() for field in fields if row.get(field, '').strip()), '')
        if text:
            return resolved(text, sid, locale)
        return talent_text(sid, locale)

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
        attach(condition, [condition.get('spell_id')], aura=True)
    for effect in effects:
        aura = effect.get('source_kind') in {'buff', 'debuff'} or effect.get('source_type') == 'runtime_state'
        attach(effect, effect.get('source_spell_ids', []), aura=aura, talent=not aura)
        if not effect.get('description_zh') and effect.get('talent_description_zh'):
            effect['description_zh'] = effect['talent_description_zh']
        if not effect.get('description') and effect.get('talent_description'):
            effect['description'] = effect['talent_description']
    return actor
