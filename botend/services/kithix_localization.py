"""基希克斯中文增量：只更新语言字段，保留装备数值和旧手册发布。"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from django.db import transaction
from django.utils import timezone

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import active_season
from botend.services.journal_service import compile_journal
from botend.services.journal_text import JournalText, index
from botend.services.ptr_journal_gear_overlay import _active_release_rows, _merge_catalog, _release_totals, _sync_in_progress
from botend.services.simc_benchmark_tooltip_generator import render_spell_description

INSTANCE_ID = 1324
ENCOUNTER_ID = 2896
ITEM_IDS = {280617, 280799, 280835, 281029, 281056, 281215, 281235, 281236, 281238, 281239}
LOOT_IDS = ITEM_IDS | {281615, 281213, 284159}
TOKEN = re.compile(r'\$\{[^{}]+\}|\$\d*proccooldown|\$\d*[sSmMwWtTdDuU]\d*')
VALUE = r'[+-]?\d[\d,.]*(?:–\d[\d,.]*（随职责变化）)?(?:秒)?'


def _chinese(value):
    return bool(re.search(r'[\u3400-\u9fff]', str(value or '')))


def localize_effect(effect, template, build):
    """按原模板逐 token 提取已验证值，再填入官方中文；不重新计算属性。"""
    result = deepcopy(effect)
    if not isinstance(effect, dict) or not effect.get('template') or not effect.get('description'):
        raise ValueError('装备特效缺少原始模板或已解析数值，拒绝猜测翻译')
    original, _ = render_spell_description(effect['template'], base_spell_id=effect['spell_id'], spell_queries={})
    chinese, _ = render_spell_description(template, base_spell_id=effect['spell_id'], spell_queries={})
    tokens = TOKEN.findall(original)
    if not _chinese(chinese) or Counter(tokens) != Counter(TOKEN.findall(chinese)):
        raise ValueError(f'技能 {effect["spell_id"]} 的中英文变量不一致，需重新核对数值构建')
    fragments = TOKEN.split(original)
    pattern = ''
    for offset, fragment in enumerate(fragments):
        pattern += re.escape(fragment)
        if offset < len(tokens):
            pattern += f'({VALUE})'
    match = re.fullmatch(pattern, effect['description'])
    if not match:
        raise ValueError(f'技能 {effect["spell_id"]} 的原始模板与展示数值不匹配')
    values = {}
    for token, value in zip(tokens, match.groups()):
        if token in values and values[token] != value:
            raise ValueError(f'技能 {effect["spell_id"]} 的重复变量数值冲突')
        values[token] = value
    text = TOKEN.sub(lambda match: values[match[0]], chinese).replace('秒秒', '秒')
    if '$' in text or re.search(r'[A-Za-z]{3,}', text):
        raise ValueError(f'技能 {effect["spell_id"]} 的中文仍有未解析内容')
    if Counter(re.findall(r'\d[\d,.]*', text)) != Counter(re.findall(r'\d[\d,.]*', effect['description'])):
        raise ValueError(f'技能 {effect["spell_id"]} 翻译前后的数值不一致')
    result.update(description_zh=('使用：' if effect.get('trigger_type') == 0 else '装备：') + text,
                  template_zh=template, localization_build=build)
    return result


def load_localization(path):
    raw = Path(path).read_bytes()
    payload = json.loads(raw.decode('utf-8'))
    if (payload.get('schema'), payload.get('instance_id'), payload.get('encounter_id'), payload.get('locale')) != (1, INSTANCE_ID, ENCOUNTER_ID, 'zhCN'):
        raise ValueError('中文制品身份或格式不正确')
    if not re.fullmatch(r'12\.1\.5\.\d+', payload.get('localization_build', '')):
        raise ValueError('中文来源必须是明确的 12.1.5 构建')
    tables = payload['tables']
    for name, values in tables.items():
        ids = [int(row['ID']) for row in values]
        if len(ids) != len(set(ids)):
            raise ValueError(f'制品表 {name} 含重复记录')
    items, spells = index(tables['ItemSparse']), index(tables['Spell'])
    if set(items) != LOOT_IDS or any(not _chinese(row.get('Display_lang')) for row in items.values()):
        raise ValueError('制品缺少装备中文名')
    rows, catalog, report = compile_journal(tables)
    if len(rows) != 1 or rows[0]['id'] != INSTANCE_ID or len(rows[0]['encounters']) != 1:
        raise ValueError('制品必须仅包含基希克斯破封')
    row = rows[0]
    boss = row['encounters'][0]
    if boss['id'] != ENCOUNTER_ID or not _chinese(row['description']) or not _chinese(boss['description']):
        raise ValueError('副本或首领中文描述不完整')
    if report['missing_item_ids'] or report['unlinked_sections'] or not boss['sections'] or not boss['loot']:
        raise ValueError('手册技能树或掉落数据不完整')
    for section in boss['sections']:
        if not _chinese(section['title']) or any(re.search(r'[A-Za-z]{3,}', text) for text in section['descriptions'].values()):
            raise ValueError(f'手册章节 {section["id"]} 仍有英文')
    return payload, row, catalog, items, spells, hashlib.sha256(raw).hexdigest()


def import_localization(path, *, apply=False, backup_path=None):
    payload, translated, catalog, items, spells, digest = load_localization(path)
    build = payload['localization_build']
    with transaction.atomic():
        states = JournalState.objects.select_related('active_release')
        if apply:
            states = states.select_for_update()
        state = states.filter(pk='wow-zhCN').first()
        season = active_season()
        if not state or not state.active_release or not season or not season.gear_batch_key:
            raise ValueError('请先准备活动手册和装备目录，再执行中文更新')
        if apply:
            season = SeasonMeta.objects.select_for_update().get(pk=season.pk)
            if not season.is_active or not season.gear_batch_key:
                raise ValueError('活动赛季已变化，请重新执行预览')
        if _sync_in_progress(state):
            raise ValueError('手册同步正在进行，请在同步完成后执行')
        previous = state.active_release
        rows = _active_release_rows(previous)
        old = next((row for row in rows if row['id'] == INSTANCE_ID), None)
        if old is None:
            raise ValueError('活动手册尚未导入基希克斯，请先运行原增量导入命令')
        snapshots = WowItemSnapshot.objects.filter(item_id__in=items)
        variants = WowItemVariantSnapshot.objects.filter(season=season, batch_key=season.gear_batch_key, item__item_id__in=ITEM_IDS).select_related('item')
        if apply:
            snapshots, variants = snapshots.select_for_update(), variants.select_for_update()
        snapshots = {item.item_id: item for item in snapshots}
        variants = list(variants)
        if {variant.item.item_id for variant in variants} != ITEM_IDS:
            raise ValueError('活动目录缺少基希克斯装备变体，请先完成原增量导入')
        item_changes, variant_changes, warnings = [], [], []
        item_facts = index(payload['tables']['Item'])
        for iid, data in items.items():
            current = snapshots.get(iid)
            if not current:
                # 非装备掉落只补基础中文事实，不凭空制造装备变体。
                if iid in ITEM_IDS:
                    raise ValueError(f'中央目录缺少装备 {iid}')
                facts = item_facts.get(iid, {})
                current = WowItemSnapshot(item_id=iid, source='wago-db2-journal', catalog_type='misc',
                                          quality=int(data.get('OverallQualityID') or 0),
                                          item_class_id=int(facts.get('ClassID') or 0),
                                          item_subclass_id=int(facts.get('SubclassID') or 0))
            metadata = deepcopy(current.metadata or {})
            metadata['localization'] = {'build': build, 'locale': 'zhCN', 'artifact_sha256': digest}
            fields = {'name_zh': data['Display_lang'], 'description_zh': data.get('Description_lang') or '', 'metadata': metadata}
            if current.inventory_type and current.inventory_type != int(data.get('InventoryType') or 0):
                warnings.append(f'物品 {iid} 新旧构建部位不同；仅更新中文，保留原装备部位与属性')
            if any(getattr(current, key) != value for key, value in fields.items()):
                item_changes.append((current, fields))
        for variant in variants:
            effects = []
            for effect in variant.effects_json:
                template = spells.get(int(effect.get('spell_id') or 0), {}).get('Description_lang')
                if not template:
                    raise ValueError(f'物品 {variant.item.item_id} 的特效没有官方中文')
                effects.append(localize_effect(effect, template, build))
            if not effects:
                raise ValueError(f'物品 {variant.item.item_id} 的现有特效不完整')
            sources = deepcopy(variant.source_json)
            for source in sources:
                if isinstance(source, dict) and int(source.get('instance_id') or 0) == INSTANCE_ID:
                    source.update(instance_zh=translated['name'], encounter_zh=translated['encounters'][0]['name'])
            fields = {'effects_json': effects, 'source_json': sources}
            if any(getattr(variant, key) != value for key, value in fields.items()):
                variant_changes.append((variant, fields))
        # 描述使用最新中文手册；原有掉落关系、难度、部位保持不变。
        localized = deepcopy(old)
        localized.update(name=translated['name'], description=translated['description'])
        translated_boss = translated['encounters'][0]
        resolver = JournalText(payload['tables'])
        for boss in localized['encounters']:
            if boss['id'] != ENCOUNTER_ID:
                raise ValueError('目标副本出现未知首领，拒绝覆盖')
            boss.update(name=translated_boss['name'], description=translated_boss['description'],
                        creatures=translated_boss['creatures'])
            sections = {section['id']: section for section in translated_boss['sections']}
            for section in boss['sections']:
                replacement = sections.get(section['id'])
                if replacement is None:
                    raise ValueError(f'旧手册章节 {section["id"]} 没有匹配的中文条目')
                for field in ('title', 'source_text'):
                    section[field] = deepcopy(replacement[field])
                section['descriptions'], section['dynamic'] = {}, {}
                for difficulty in section['difficulty_ids']:
                    text, missing = resolver.resolve(section['source_text'], section['spell_id'], difficulty)
                    section['descriptions'][str(difficulty)] = text
                    if missing:
                        section['dynamic'][str(difficulty)] = missing
            for drop in boss['loot']:
                data = items.get(drop['item_id'])
                if data is None:
                    raise ValueError(f'掉落 {drop["item_id"]} 缺少中文')
                drop.update(name=data['Display_lang'], description=data.get('Description_lang') or '', source='wago')
        manifest = deepcopy(previous.manifest or {})
        merged_catalog = _merge_catalog(manifest.get('catalog'), catalog)
        current_tiers = [tier['id'] for tier in merged_catalog['tiers'] if tier['order'] == 9000]
        if len(current_tiers) != 1:
            raise ValueError('无法唯一确定本赛季手册目录')
        localized['tier_ids'] = sorted(set(localized['tier_ids']) | set(current_tiers))
        manifest['catalog'] = merged_catalog
        override = {'season_key': season.season_key, 'localization_build': build, 'artifact_sha256': digest}
        manifest.setdefault('display_overrides', {})[str(INSTANCE_ID)] = override
        # 保留数值来源构建：手册 tooltip 仍选择原有精确构建的装备变体。
        rows = [localized if row['id'] == INSTANCE_ID else row for row in rows]
        journal_changed = localized != old or manifest != previous.manifest
        report = {'已写入': False, '无需重复更新': not (journal_changed or item_changes or variant_changes),
                  '中文来源版本': build, '赛季': season.season_key, '装备批次': season.gear_batch_key,
                  '更新物品数': len(item_changes), '更新变体数': len(variant_changes),
                  '更新手册': journal_changed, '提示': warnings, '制品摘要': digest}
        if not apply or report['无需重复更新']:
            return report
        if not backup_path:
            raise ValueError('实际更新必须提供备份文件路径')
        backup = {'说明': '仅备份本次要修改的字段；旧手册发布完整保留，可恢复 active_release_id。',
                  'previous_release_id': previous.pk, 'season_id': season.pk, 'batch_key': season.gear_batch_key,
                  'items': [{'pk': obj.pk, 'item_id': obj.item_id, 'created': obj.pk is None,
                             'fields': {key: getattr(obj, key) for key in fields}} for obj, fields in item_changes],
                  'variants': [{'pk': obj.pk, 'fields': {key: getattr(obj, key) for key in fields}} for obj, fields in variant_changes]}
        backup_path = Path(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open('x', encoding='utf-8') as stream:
            json.dump(backup, stream, ensure_ascii=False, indent=2)
        for obj, fields in item_changes + variant_changes:
            for key, value in fields.items():
                setattr(obj, key, value)
            obj.updated_at = timezone.now()
            obj.save(update_fields=[*fields, 'updated_at'] if obj.pk else None)
        if journal_changed:
            release = JournalRelease.objects.create(build=previous.build, locale='zhCN', status='completed',
                                                    manifest=manifest, report={**previous.report, **_release_totals(rows)},
                                                    completed_at=timezone.now())
            for row in rows:
                bosses = row.pop('encounters')
                instance = JournalInstance.objects.create(release=release, journal_id=row['id'], name=row['name'],
                                                          kind=row['kind'], expansion=row['expansion'], payload=row)
                JournalEncounter.objects.bulk_create([JournalEncounter(instance=instance, journal_id=boss['id'],
                    name=boss['name'], order=boss['order'], payload=boss) for boss in bosses])
            state.active_release = release
            state.save(update_fields=['active_release'])
        report.update({'已写入': True, '备份': str(backup_path), '旧手册发布': previous.pk,
                       '当前手册发布': state.active_release_id})
        return report
