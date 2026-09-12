"""将版本化 PTR 冒险手册/装备 overlay 增量并入当前线上快照。"""
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


OVERLAY_SCHEMA = 1


def _load_artifact(path):
    path = Path(path)
    raw = path.read_bytes()
    payload = json.loads(raw.decode('utf-8'))
    if payload.get('schema') != OVERLAY_SCHEMA:
        raise ValueError(f'不支持的 PTR overlay schema：{payload.get("schema")}')
    build = str(payload.get('source_build') or '').strip()
    journal = payload.get('journal') or {}
    row = journal.get('row') or {}
    gear = (payload.get('gear') or {}).get('items') or []
    if not build or int(payload.get('instance_id') or 0) != int(row.get('id') or 0):
        raise ValueError('PTR overlay 的 build 或实例身份不完整')
    encounters = row.get('encounters') or []
    if not encounters or not gear:
        raise ValueError('PTR overlay 缺少首领或装备数据')
    encounter_ids = [int(encounter.get('id') or 0) for encounter in encounters]
    if 0 in encounter_ids or len(encounter_ids) != len(set(encounter_ids)):
        raise ValueError('PTR overlay 含有无效或重复的首领 ID')
    item_ids = [int(item.get('item_id') or 0) for item in gear]
    if 0 in item_ids or len(item_ids) != len(set(item_ids)):
        raise ValueError('PTR overlay 含有无效或重复的装备 ID')
    for item in gear:
        item_id = int(item['item_id'])
        icon = str(item.get('icon') or '').strip()
        if not re.fullmatch(r'[a-z0-9_]+', icon):
            raise ValueError(f'物品 {item_id} 的图标名不安全')
        variants = item.get('variants') or []
        variant_keys = [str(variant.get('key') or '').strip() for variant in variants]
        if not variants or '' in variant_keys or len(variant_keys) != len(set(variant_keys)):
            raise ValueError(f'物品 {item_id} 含有无效或重复的变体身份')
        if item.get('name_zh') and not any('\u3400' <= char <= '\u9fff' for char in item['name_zh']):
            raise ValueError(f'物品 {item_id} 的中文名不含中文字符')
        for variant in variants:
            metadata = variant.get('metadata') or {}
            if not metadata.get('ptr_preview') or metadata.get('stats_status') != 'awaiting_same_build_simc':
                raise ValueError(f'物品 {item_id} 的 PTR 预览状态不完整')
            if variant.get('stats') or variant.get('effects'):
                raise ValueError(f'物品 {item_id} 在同构建 SimC 可用前不得写入猜测属性或特效')
    return payload, build, row, gear, hashlib.sha256(raw).hexdigest()


def _merge_catalog(current, overlay):
    merged = deepcopy(current or {})
    merged['art_ids'] = sorted(set(merged.get('art_ids') or []) | set(overlay.get('art_ids') or []))
    for key in ('tiers', 'difficulties'):
        rows = {int(row['id']): deepcopy(row) for row in merged.get(key) or []}
        rows.update({int(row['id']): deepcopy(row) for row in overlay.get(key) or []})
        merged[key] = [rows[row_id] for row_id in sorted(rows)]
    return merged


def _release_totals(instances):
    encounters = [encounter for instance in instances for encounter in instance['encounters']]
    return {
        'instances': len(instances),
        'encounters': len(encounters),
        'sections': sum(len(row.get('sections') or []) for row in encounters),
        'loot': sum(len(row.get('loot') or []) for row in encounters),
    }


def _active_release_rows(release):
    rows = []
    for instance in release.instances.prefetch_related('encounters').all():
        row = deepcopy(instance.payload or {})
        row.update({
            'id': instance.journal_id,
            'name': instance.name,
            'kind': instance.kind,
            'expansion': instance.expansion,
            'encounters': [deepcopy(encounter.payload or {}) for encounter in instance.encounters.all()],
        })
        rows.append(row)
    return rows


def _item_defaults(item, existing=None):
    existing = existing or None
    metadata = deepcopy(existing.metadata if existing else {})
    metadata.update(deepcopy(item.get('metadata') or {}))
    return {
        'name': item.get('name') or (existing.name if existing else ''),
        'name_zh': item.get('name_zh') or (existing.name_zh if existing else ''),
        'description': item.get('description') or (existing.description if existing else ''),
        'description_zh': item.get('description_zh') or (existing.description_zh if existing else ''),
        'icon': item.get('icon') or (existing.icon if existing else ''),
        'quality': int(item.get('quality') or 0),
        'source': 'wago-ptr-db2',
        'catalog_type': item.get('catalog_type') or 'equipment',
        'inventory_type': int(item.get('inventory_type') or 0),
        'slot_key': item.get('slot_key') or '',
        'item_class_id': int(item.get('item_class_id') or 0),
        'item_subclass_id': int(item.get('item_subclass_id') or 0),
        'armor_type': item.get('armor_type') or '',
        'weapon_type': item.get('weapon_type') or '',
        'allowable_class_mask': int(item.get('allowable_class_mask') or 0),
        'eligible_specs': deepcopy(item.get('eligible_specs') or []),
        'unique_group': item.get('unique_group') or '',
        'effect_refs': deepcopy(item.get('effect_refs') or []),
        'simc_token': item.get('simc_token') or '',
        'metadata': metadata,
        'updated_at': timezone.now(),
    }


def _write_gear(gear, season, build):
    variant_count = 0
    for item_data in gear:
        item_id = int(item_data['item_id'])
        existing = WowItemSnapshot.objects.filter(item_id=item_id).first()
        item, _ = WowItemSnapshot.objects.update_or_create(
            item_id=item_id,
            defaults=_item_defaults(item_data, existing),
        )
        for variant in item_data['variants']:
            WowItemVariantSnapshot.objects.update_or_create(
                season=season,
                batch_key=season.gear_batch_key,
                item=item,
                variant_key=variant['key'],
                defaults={
                    'game_build': build,
                    'variant_type': variant.get('type') or WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                    'item_level': int(variant.get('item_level') or 0),
                    'upgrade_track': variant.get('upgrade_track') or '',
                    'track_rank': int(variant.get('track_rank') or 0),
                    'track_max_rank': int(variant.get('track_max_rank') or 0),
                    'bonus_ids': deepcopy(variant.get('bonus_ids') or []),
                    'compatible_slots': deepcopy(variant.get('compatible_slots') or []),
                    'socket_types': deepcopy(variant.get('socket_types') or []),
                    'socket_count': int(variant.get('socket_count') or 0),
                    'stats_json': deepcopy(variant.get('stats') or {}),
                    'effects_json': deepcopy(variant.get('effects') or []),
                    'source_json': deepcopy(variant.get('sources') or []),
                    'metadata': deepcopy(variant.get('metadata') or {}),
                },
            )
            variant_count += 1
    return variant_count


def _gear_overlay_is_complete(gear, season, build):
    expected = {
        (int(item['item_id']), str(variant['key']))
        for item in gear for variant in item['variants']
    }
    rows = WowItemVariantSnapshot.objects.filter(
        season=season,
        batch_key=season.gear_batch_key,
        item__item_id__in=[int(item['item_id']) for item in gear],
    ).values_list('item__item_id', 'variant_key', 'game_build', 'metadata')
    actual = {}
    for item_id, variant_key, game_build, metadata in rows:
        metadata = metadata if isinstance(metadata, dict) else {}
        if (metadata.get('ptr_preview')
                and metadata.get('stats_status') == 'awaiting_same_build_simc'):
            actual[(int(item_id), str(variant_key))] = str(game_build or '')
    return expected == set(actual) and all(actual[key] == build for key in expected)


def _sync_in_progress(state):
    return bool(state.sync_token and state.sync_until and state.sync_until > timezone.now())


def import_ptr_journal_gear_overlay(path, *, apply=False):
    """校验 overlay；apply=True 时原子合并冒险手册并追加到活动装备批次。"""
    payload, build, overlay_row, gear, artifact_hash = _load_artifact(path)
    state = JournalState.objects.select_related('active_release').filter(pk='wow-zhCN').first()
    if not state or not state.active_release:
        raise ValueError('当前没有可供增量合并的冒险手册 release')
    if _sync_in_progress(state):
        raise ValueError('冒险手册完整同步正在进行，暂不允许叠加 PTR 数据')
    season = active_season()
    if not season or not season.gear_batch_key:
        raise ValueError('当前没有可追加 PTR 装备的活动装备批次')
    artifact_item_ids = [int(item['item_id']) for item in gear]
    batch_variants = WowItemVariantSnapshot.objects.filter(
        season=season,
        batch_key=season.gear_batch_key,
    )
    if not batch_variants.exclude(item__item_id__in=artifact_item_ids).exists():
        raise ValueError('活动装备批次没有现存正式服装备，拒绝将 PTR 预览作为完整目录发布')

    current_release_id = state.active_release_id
    current_batch_key = season.gear_batch_key
    current_rows = _active_release_rows(state.active_release)
    target_id = int(overlay_row['id'])
    replaced = sum(int(row['id']) == target_id for row in current_rows)
    candidate_rows = [row for row in current_rows if int(row['id']) != target_id]
    candidate_rows.append(deepcopy(overlay_row))
    totals = _release_totals(candidate_rows)
    overlay_metadata = ((state.active_release.manifest or {}).get('ptr_overlays') or {}).get(str(target_id)) or {}
    already_applied = bool(
        overlay_metadata.get('source_build') == build
        and overlay_metadata.get('artifact_sha256') == artifact_hash
        and _gear_overlay_is_complete(gear, season, build)
    )
    report = {
        'applied': bool(apply and not already_applied),
        'already_applied': already_applied,
        'artifact_sha256': artifact_hash,
        'source_build': build,
        'journal': {'added': 0 if replaced else 1, 'replaced': replaced, **totals},
        'gear': {'items': len(gear), 'variants': sum(len(item['variants']) for item in gear),
                 'season_key': season.season_key, 'batch_key': season.gear_batch_key},
    }
    if already_applied or not apply:
        return report

    with transaction.atomic():
        state = JournalState.objects.select_for_update().select_related('active_release').get(pk='wow-zhCN')
        season = SeasonMeta.objects.select_for_update().get(pk=season.pk)
        if _sync_in_progress(state):
            raise ValueError('冒险手册完整同步正在进行，暂不允许叠加 PTR 数据')
        if state.active_release_id != current_release_id or season.gear_batch_key != current_batch_key:
            raise ValueError('活动快照已变化，请重新执行 dry-run')
        if not WowItemVariantSnapshot.objects.filter(
                season=season, batch_key=season.gear_batch_key,
        ).exclude(item__item_id__in=artifact_item_ids).exists():
            raise ValueError('活动装备批次已变化，未找到可保留的正式服装备')
        previous = state.active_release
        manifest = deepcopy(previous.manifest or {})
        manifest['catalog'] = _merge_catalog(manifest.get('catalog'), (payload['journal'].get('catalog') or {}))
        overlays = deepcopy(manifest.get('ptr_overlays') or {})
        overlays[str(target_id)] = {
            'source_build': build,
            'artifact_sha256': artifact_hash,
            'manifest': deepcopy(payload['journal'].get('manifest') or {}),
        }
        manifest['ptr_overlays'] = overlays
        release_report = deepcopy(previous.report or {})
        release_report.update(totals)
        release_report['ptr_overlays'] = sorted(int(key) for key in overlays)
        base_build = previous.build.split('+ptr-', 1)[0]
        release = JournalRelease.objects.create(
            build=f'{base_build}+ptr-{build}',
            locale=previous.locale,
            status='completed',
            manifest=manifest,
            report=release_report,
            completed_at=timezone.now(),
        )
        for row in candidate_rows:
            encounters = row.pop('encounters')
            instance = JournalInstance.objects.create(
                release=release,
                journal_id=int(row['id']),
                name=row['name'],
                kind=row['kind'],
                expansion=int(row.get('expansion') or 0),
                payload=row,
            )
            JournalEncounter.objects.bulk_create([
                JournalEncounter(
                    instance=instance,
                    journal_id=int(encounter['id']),
                    name=encounter['name'],
                    order=int(encounter.get('order') or 0),
                    payload=encounter,
                )
                for encounter in encounters
            ])
        written_variants = _write_gear(gear, season, build)
        if written_variants != report['gear']['variants']:
            raise ValueError('PTR overlay 装备变体写入数量不完整')
        state.active_release = release
        state.save(update_fields=['active_release'])
    return report
