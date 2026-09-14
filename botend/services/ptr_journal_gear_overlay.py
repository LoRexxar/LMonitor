"""将版本化 PTR 冒险手册/装备 overlay 增量并入当前线上快照。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import SeasonMeta, WowItemVariantSnapshot
from botend.services.gear_builder import active_season
from botend.services.wow_item_catalog_import import (
    exact_build_variant_payload_is_complete,
    item_catalog_is_complete,
    upsert_item_catalog,
    validate_item_catalog_payload,
)


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
    catalog_art_ids = {
        int(value) for value in ((journal.get('catalog') or {}).get('art_ids') or [])
    }
    missing_art_ids = sorted(_catalog_references(row)[2] - catalog_art_ids)
    if missing_art_ids:
        raise ValueError(f'PTR overlay 图片白名单缺少实际引用：{missing_art_ids}')
    encounters = row.get('encounters') or []
    if not encounters or not gear:
        raise ValueError('PTR overlay 缺少首领或装备数据')
    encounter_ids = [int(encounter.get('id') or 0) for encounter in encounters]
    if 0 in encounter_ids or len(encounter_ids) != len(set(encounter_ids)):
        raise ValueError('PTR overlay 含有无效或重复的首领 ID')
    validate_item_catalog_payload(gear, build)
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


def _catalog_references(row):
    tier_ids = {int(value) for value in row.get('tier_ids') or []}
    difficulty_ids = set()
    art_ids = set()

    def walk(value, key=''):
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, child_key)
        elif isinstance(value, list):
            if key == 'difficulty_ids':
                difficulty_ids.update(int(item) for item in value if str(item).isdigit())
            else:
                for child in value:
                    walk(child, key)
        elif key in {'image', 'background', 'icon'} and str(value).isdigit():
            art_ids.add(int(value))

    walk(row)
    return tier_ids, difficulty_ids, art_ids - {0}


def _legacy_overlay_catalog(active_release, row):
    """从旧版混合 catalog 精确投影单个 PTR 实例仍引用的目录项。"""
    catalog = (active_release.manifest or {}).get('catalog') or {}
    tier_ids, difficulty_ids, art_ids = _catalog_references(row)
    allowed_art = {int(value) for value in catalog.get('art_ids') or []}
    return {
        'art_ids': sorted(art_ids & allowed_art),
        'tiers': [deepcopy(item) for item in catalog.get('tiers') or [] if int(item.get('id') or 0) in tier_ids],
        'difficulties': [
            deepcopy(item) for item in catalog.get('difficulties') or []
            if int(item.get('id') or 0) in difficulty_ids
        ],
    }


def preserve_active_ptr_journal_overlay(active_release, rows, catalog, report, retail_build):
    """把活动 release 的 PTR 实例投影到新的正式服 release。

    正式服数据优先：若正式服已包含同一 JournalInstanceID，则丢弃旧 PTR
    overlay，避免预览数据永久压过后来发布的正式数据。
    """
    overlays = dict((active_release.manifest or {}).get('ptr_overlays') or {}) if active_release else {}
    if not overlays:
        return rows, catalog, report, {}, retail_build

    official_ids = {int(row['id']) for row in rows}
    active_rows = {int(row['id']): row for row in _active_release_rows(active_release)}
    retained = {}
    for key, metadata in overlays.items():
        instance_id = int(key)
        if instance_id in official_ids or instance_id not in active_rows:
            continue
        retained_row = deepcopy(active_rows[instance_id])
        rows.append(retained_row)
        metadata = metadata if isinstance(metadata, dict) else {}
        overlay_catalog = metadata.get('catalog') or _legacy_overlay_catalog(active_release, retained_row)
        catalog = _merge_catalog(catalog, overlay_catalog)
        retained[str(instance_id)] = deepcopy(metadata)
        retained[str(instance_id)]['catalog'] = deepcopy(overlay_catalog)

    if not retained:
        return rows, catalog, report, {}, retail_build

    report = dict(report)
    report.update(_release_totals(rows))
    report['ptr_overlays'] = sorted(map(int, retained))
    ptr_builds = sorted({str(row.get('source_build') or '') for row in retained.values()} - {''})
    combined_build = retail_build + ''.join(f'+ptr-{build}' for build in ptr_builds)
    return rows, catalog, report, retained, combined_build


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
        and item_catalog_is_complete(gear, season, build)
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
            'catalog': deepcopy(payload['journal'].get('catalog') or {}),
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
        written_variants = upsert_item_catalog(gear, season, build)
        if written_variants != report['gear']['variants']:
            raise ValueError('PTR overlay 装备变体写入数量不完整')
        state.active_release = release
        state.save(update_fields=['active_release'])
    return report
