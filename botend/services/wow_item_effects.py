"""中央装备特效：共用语言模板，变体只引用模板并携带已采集参数。

读取不写库、不访问网络。旧原文可在内存中投影；回填与采集发布使用同一写入函数。
没有可靠的双语对应关系时只展示有来源的中文参考，不猜测翻译或搬用其他装等数字。
"""
from copy import deepcopy
import hashlib
import json
import re

SCHEMA = 1
NUMBER = re.compile(r'(?<![A-Za-z_$])\d[\d,]*(?:\.\d+)?')
CJK = re.compile(r'[\u3400-\u9fff]')


def _is_chinese(text):
    # 英文采集器可能已经将时间单位替换成“秒”；单位不能证明正文已汉化。
    return bool(CJK.search(re.sub(r'秒|分钟|小时', '', str(text or ''))))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _text(row):
    if isinstance(row, str):
        return {'description_zh' if _is_chinese(row) else 'description': row.strip()}
    return deepcopy(row) if isinstance(row, dict) else {}


def _display_text(row):
    return row.get('description_zh') or row.get('name_zh') or row.get('description') or row.get('name') or ''


def _shape(text):
    values = {}
    def replace(match):
        key = f'v{len(values)}'
        values[key] = match[0]
        return '{' + key + '}'
    return NUMBER.sub(replace, str(text or '').strip()), values


def _render(template, values):
    return re.sub(r'\{(v\d+)\}', lambda m: str(values.get(m[1], '数值待确认')), template)


def _capture(template, text):
    parts = re.split(r'(\{v\d+\})', template)
    pattern, keys = '', []
    for part in parts:
        if re.fullmatch(r'\{v\d+\}', part):
            keys.append(part[1:-1])
            pattern += r'(\d[\d,]*(?:\.\d+)?)'
        else:
            pattern += re.escape(part)
    match = re.fullmatch(pattern, str(text or '').strip())
    return dict(zip(keys, match.groups())) if match else None


def _definition(raw):
    row = _text(raw)
    zh = str(row.get('description_zh') or row.get('name_zh') or '').strip()
    en = str(row.get('description') or row.get('name') or '').strip()
    if not zh and _is_chinese(en):
        zh, en = en, ''
    if not zh and not en:
        return None, {}
    zh_shape, zh_values = _shape(zh)
    en_shape, en_values = _shape(en)
    # 同一来源的双语数值次序一致时才共享位置参数；否则只保留各语言原形。
    paired = bool(zh and en and list(zh_values.values()) == list(en_values.values())
                  and len(set(en_values.values())) == len(en_values))
    shape = en_shape if en else zh_shape
    identity = {'spell_id': row.get('spell_id'), 'trigger_type': row.get('trigger_type'), 'shape': shape}
    key = digest(identity)[:32]
    definition = {'key': key, 'template_zh': zh_shape, 'template_en': en_shape,
                  'paired': paired, 'spell_id': row.get('spell_id'),
                  'trigger_type': row.get('trigger_type')}
    # 显式 DB2 模板保留变量身份，可跨装等重用；不把 useText 序号当技能 ID。
    if row.get('template') and row.get('template_zh') and row.get('spell_id'):
        definition.update(spell_template=row['template'], spell_template_zh=row['template_zh'])
    return definition, zh_values if zh else en_values


def compile_effects(variants):
    """从给定当前目录构建唯一公共资料和变体引用，不改变原始采集记录。"""
    from botend.services.wow_data_branch import variant_branch
    branches, references = {}, {}
    for variant in sorted(variants, key=lambda row: (row.item_level, row.pk or 0)):
        branch = variant_branch(variant)
        definitions = branches.setdefault(branch, {})
        refs = []
        for raw in variant.effects_json or []:
            definition, values = _definition(raw)
            if definition is None:
                refs.append({})
                continue
            key = definition['key']
            previous = definitions.get(key)
            # 同一英文机制只补中文，不允许后来的英文响应擦掉已有中文。
            if previous and previous.get('template_zh') and not definition['template_zh']:
                definition = previous
            elif previous and previous.get('template_zh') and definition['template_zh'] != previous['template_zh']:
                # 冲突文本分别保留，不能静默覆盖成为跨变体翻译。
                previous['conflicted'] = definition['conflicted'] = True
                key = digest([key, definition['template_zh']])[:32]
                definition['key'] = key
            definitions[key] = definition
            source = (variant.metadata or {}).get('tooltip_source')
            definition.setdefault('source', {'item_id': variant.item.item_id,
                'provider': source.get('provider', 'catalog') if isinstance(source, dict) else 'catalog',
                'text_hash': digest(raw)})
            refs.append({'key': key, 'values': values,
                         'language': 'zhCN' if _is_chinese(_display_text(_text(raw))) else 'enUS'})
        references[variant.pk] = {'schema': SCHEMA, 'branch': branch,
                                 'source_hash': digest(variant.effects_json or []), 'effects': refs}
    for definitions in branches.values():
        groups = {}
        for definition in definitions.values():
            group = (definition.get('spell_id'), definition.get('trigger_type'), definition['template_en'])
            if definition['template_en']:
                groups.setdefault(group, []).append(definition)
        for group in groups.values():
            if len({row['template_zh'] for row in group if row['template_zh']}) > 1:
                for definition in group:
                    definition['conflicted'] = True
    document = {'schema': SCHEMA, 'branches': {
        branch: {'definitions': {key: rows[key] for key in sorted(rows)}}
        for branch, rows in sorted(branches.items())}}
    return document, references


def _spell_translation(row, definition):
    if not (definition.get('spell_id') and (not row.get('spell_id') or row['spell_id'] == definition.get('spell_id'))
            and definition.get('spell_template') and definition.get('spell_template_zh')):
        return None
    from botend.services.kithix_localization import localize_effect
    candidate = {**row, 'spell_id': definition['spell_id'],
                 'trigger_type': definition.get('trigger_type'), 'template': definition['spell_template']}
    candidate['description'] = re.sub(r'^(?:Equip|Use):\s*', '', candidate.get('description', ''), flags=re.I)
    try:
        return localize_effect(candidate, definition['spell_template_zh'], 'central')['description_zh']
    except (ValueError, KeyError):
        return None


def resolve_effects(item, variant=None, *, branch='retail', raw_effects=None,
                    allow_reference=False, known_empty=False, document=None, item_id=None):
    """语言完整度与数值完整度独立；不同分支及不同机制不能互相补值。"""
    document = document if document is not None else getattr(item, 'effects', {})
    definitions = ((document or {}).get('branches', {}).get(branch, {}).get('definitions', {})
                   if isinstance(document, dict) and document.get('schema') == SCHEMA else {})
    if raw_effects is None and variant is not None:
        from botend.services.journal_effect_supplement import supplemental_effects
        raw_effects = supplemental_effects(variant)
    raw = list(raw_effects or [])
    if known_empty and not raw:
        return [], 'none'
    result = []
    references = getattr(variant, 'effect_values', {}) or {}
    references = references.get('effects', []) if (references.get('schema') == SCHEMA
        and references.get('branch') == branch and references.get('source_hash') == digest(raw)) else []
    for index, value in enumerate(raw):
        row = _text(value)
        reference = references[index] if index < len(references) else {}
        definition = definitions.get(reference.get('key'), {})
        if definition.get('template_zh') and (reference.get('language') == 'zhCN' or (
                definition.get('paired') and not definition.get('conflicted'))):
            row['description_zh'] = _render(definition['template_zh'], reference.get('values', {}))
            row['effect_key'] = reference['key']
        if _is_chinese(_display_text(row)):
            result.append(row)
            continue
        translations = set()
        for definition in definitions.values():
            if definition.get('conflicted'):
                continue
            # 显式技能身份冲突时即使英文相同也不能替换。
            if row.get('spell_id') and definition.get('spell_id') != row['spell_id']:
                continue
            if row.get('trigger_type') is not None and definition.get('trigger_type') not in (None, row['trigger_type']):
                continue
            translated = _spell_translation(row, definition)
            if translated:
                translations.add(translated)
            if definition.get('paired'):
                values = _capture(definition['template_en'], row.get('description') or row.get('name'))
                if values is not None:
                    translations.add(_render(definition['template_zh'], values))
        if len(translations) == 1:
            row['description_zh'] = next(iter(translations))
        result.append(row)
    if result:
        # 线上补充模板也走中央读取，保留当前数值和分支，不回写采集原文。
        from botend.services.item_effect_localization import localize_item_effects
        result = localize_item_effects(item_id or getattr(item, 'item_id', None), result, branch=branch)
        status = 'localized' if all(_is_chinese(_display_text(row)) for row in result) else 'untranslated'
        return result, status
    if not allow_reference:
        return [], 'unknown'
    # 没有当前变体时展示共用机制，所有采集数值均置为待确认，不假装精确匹配。
    seen = set()
    for definition in definitions.values():
        template = definition.get('template_zh')
        if not template:
            continue
        text = _render(template, {})
        if text not in seen:
            seen.add(text)
            result.append({'description_zh': text, 'effect_key': definition['key'],
                           'values_status': 'unknown', 'applicability_status': 'reference'})
    return result, 'reference' if result else 'unknown'


def sync_central_effects(*, season=None, item_ids=None, apply=True, notify=True):
    """批量归并当前资料，仅写变化字段；可用于回填预检和采集提交。"""
    from django.db import transaction
    from django.utils import timezone
    from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
    from botend.services.gear_builder import active_season
    season = season or active_season()
    report = {'items': 0, 'variants': 0, 'untranslated': 0}
    if not season:
        return report
    with transaction.atomic():
        if apply:
            locked = SeasonMeta.objects.select_for_update().get(pk=season.pk)
            if locked.gear_batch_key != season.gear_batch_key:
                raise ValueError('装备批次已变化，请重新预检中央特效')
        items = WowItemSnapshot.objects.filter(gear_variants__season=season,
            gear_variants__batch_key=season.gear_batch_key).distinct().order_by('pk')
        if item_ids is not None:
            items = items.filter(item_id__in=set(item_ids))
        # 分批调用方控制规模；锁顺序与目录发布一致，先物品再变体。
        if apply:
            items = items.select_for_update()
        items = list(items)
        variants = WowItemVariantSnapshot.objects.filter(season=season,
            batch_key=season.gear_batch_key, item_id__in=[row.pk for row in items]).select_related('item').order_by('pk')
        if apply:
            variants = variants.select_for_update()
        grouped = {}
        for row in variants:
            grouped.setdefault(row.item_id, []).append(row)
        changed_items, changed_variants = [], []
        for item in items:
            rows = grouped.get(item.pk, [])
            document, references = compile_effects(rows)
            # 已补充的中央中文不会被后续仅英文的采集擦除。
            old = item.effects if isinstance(item.effects, dict) and item.effects.get('schema') == SCHEMA else {}
            for branch, body in document['branches'].items():
                previous = old.get('branches', {}).get(branch, {}).get('definitions', {})
                for key, definition in body['definitions'].items():
                    older = previous.get(key, {})
                    if older.get('template_zh') and (not definition.get('template_zh')
                            or older.get('source') == definition.get('source')):
                        # 同一来源未变时保留中央修订；新的仅英文响应也不能清空已有中文。
                        preserved = deepcopy(older)
                        if definition.get('conflicted'):
                            preserved['conflicted'] = True
                        body['definitions'][key] = preserved
            report['untranslated'] += sum(not definition.get('template_zh')
                for body in document['branches'].values() for definition in body['definitions'].values())
            if item.effects != document:
                item.effects, item.updated_at = document, timezone.now()
                changed_items.append(item)
            for row in rows:
                if row.effect_values != references[row.pk]:
                    row.effect_values = references[row.pk]
                    changed_variants.append(row)
        report.update(items=len(changed_items), variants=len(changed_variants))
        if apply:
            if changed_items:
                WowItemSnapshot.objects.bulk_update(changed_items, ['effects', 'updated_at'])
            if changed_variants:
                WowItemVariantSnapshot.objects.bulk_update(changed_variants, ['effect_values'])
            if (changed_items or changed_variants) and notify:
                transaction.on_commit(notify_effects_changed)
    return report


def notify_effects_changed():
    """中央资料发布后失效展示缓存，不改动模拟输入或结果。"""
    from botend.models import SimcBenchmarkPanel
    from botend.services.gear_catalog_snapshot import invalidate_catalog_snapshots
    from botend.services.simc_benchmark_result_snapshot import invalidate_result_snapshot
    invalidate_catalog_snapshots()
    for panel_id in SimcBenchmarkPanel.objects.filter(is_active=True).values_list('pk', flat=True):
        invalidate_result_snapshot(panel_id)
