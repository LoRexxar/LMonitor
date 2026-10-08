"""同分支单份数据的增量更新：校验候选后原位替换，失败保留当前数据。"""
import csv
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import current_thread, main_thread

from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from botend.models import (MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost, SeasonMeta,
                           WowDataUpdateState, WowDataUpdateRun, WowItemSnapshot,
                           WowItemVariantSnapshot, WowTalentVersion, WowTalentNodeMetadata)
from botend.plugin_sync import claim_monitor_task, renew_monitor_task_lease, release_monitor_task_lease
from botend.services.gear_builder import active_season
from botend.services.gear_builder_catalog_source import CurrentGearCatalogSource, RAIDBOTS_LIVE_ROOT
from botend.services.journal_source import WagoJournalSource, inertia
from botend.management.commands.sync_gear_builder_catalog import Command as CatalogImporter
from botend.wow.talents.grants import DB2_GRANT_TABLES
from botend.services.wow_data_branch import variant_branch

TASK_NAME = 'WowDataVersionMonitor'
TASK_TYPE = 37
# 显式绑定产品，禁止把测试服最新号当成正式服号。
BRANCHES = {'retail': ('wow', 'live', 'live'), 'ptr': ('wowxptr', 'ptr', 'ptr-2'),
            'beta': ('wow_beta', 'beta', 'beta')}
DEFAULT_FLAGS = ('is_default_simulator', 'is_default_player_tree', 'is_default_stats')
TALENT_TABLES = tuple(dict.fromkeys((
    'TraitNode', 'TraitNodeXTraitNodeEntry', 'TraitNodeEntry', 'TraitDefinition', 'TraitEdge',
    'TraitSubTree', 'SpellMisc', 'SpellEffect', 'SpellDuration', 'SpellRadius', 'SpellRange',
    'SpellAuraOptions', 'SpellTargetRestrictions', *DB2_GRANT_TABLES,
)))


def build_number(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', str(value or '')):
        raise ValueError('上游未提供完整四段构建号')
    return tuple(map(int, value.split('.')))


def get_monitor_task():
    return MonitorTask.objects.get_or_create(name=TASK_NAME, defaults={
        'type': TASK_TYPE, 'target': 'https://wago.tools/builds', 'is_active': True,
        'wait_time': 43200, 'last_scan_time': timezone.now(),
        'notes': '每 12 小时检测已启用分支；独立执行，下载并发 2，按 ID 原位更新，失败保留数据并在下一周期重试。',
    })[0]


@contextmanager
def update_lease(task=None):
    task = task or get_monitor_task()
    if task.name != TASK_NAME:
        raise ValueError('不是装备天赋更新任务')
    manual = not getattr(task, '_monitor_task_lease_owner', None)
    if manual:
        task = claim_monitor_task(task.pk, lease_seconds=1800)
        if task is None:
            raise MonitorTaskLeaseLost('已有装备天赋更新正在执行')

    def heartbeat(*_args):
        if not renew_monitor_task_lease(task.pk, task._monitor_task_lease_owner, lease_seconds=1800):
            raise MonitorTaskLeaseLost('装备天赋更新租约已失效')
    try:
        heartbeat()
        yield task, heartbeat
    finally:
        if manual:
            release_monitor_task_lease(task.pk, task._monitor_task_lease_owner)


def assert_lease(task):
    MonitorTask.objects.select_for_update().get(pk=task.pk)
    if not MonitorTaskLease.objects.select_for_update().filter(
        task_id=task.pk, owner=task._monitor_task_lease_owner, expires_at__gt=timezone.now(),
    ).exists():
        raise MonitorTaskLeaseLost('更新执行权已失效，拒绝发布')


class BranchCatalogSource(CurrentGearCatalogSource):
    """固定来源分支；各来源构建号仅记录，不要求同步升级。"""
    def __init__(self, branch, build, heartbeat, directory):
        super().__init__(cache_dir=directory, progress=heartbeat, refresh_wowhead_cache=True, workers=2)
        self.branch, self.target_build, self.heartbeat = branch, build, heartbeat

    def _get(self, url, **kwargs):
        if current_thread() is main_thread():
            self.heartbeat()
        return super()._get(url, **kwargs)

    def _get_json(self, url):
        root = f'https://www.raidbots.com/static/data/{BRANCHES[self.branch][1]}'
        return super()._get_json(url.replace(RAIDBOTS_LIVE_ROOT, root))

    def _wago_current_build(self):
        return self.target_build

    def _wowhead_tooltip(self, item_id, item_level, cache_dir):
        return self.venomstone_tooltip(item_id, item_level, cache_dir, BRANCHES[self.branch][2])

    def _tooltip_cache_directory(self, game_build):
        return self.cache_root / 'current' / 'wowhead'

    def venomstone_tooltip(self, item_id, item_level, cache_dir, branch='live'):
        details = super().venomstone_tooltip(item_id, item_level, cache_dir, BRANCHES[self.branch][2])
        if item_level and details.get('item_level') != item_level:
            raise ValueError(f'{item_id} 返回装等与请求的 {item_level} 不同')
        return details

    @staticmethod
    def _resolve_catalog_build(wago_build, raidbots_build):
        return raidbots_build or wago_build, 'branch_current'


def variant_payload(row):
    payload = {field: deepcopy(getattr(row, field)) for field in (
        'item_level', 'upgrade_track', 'track_rank', 'track_max_rank', 'crafting_quality',
        'bonus_ids', 'compatible_slots', 'socket_types', 'socket_count', 'crafting_options',
        'unique_group', 'max_equipped', 'is_intrinsic_embellishment', 'metadata',
    )}
    payload.update(key=row.variant_key, type=row.variant_type, stats=deepcopy(row.stats_json),
                   effects=deepcopy(row.effects_json), sources=deepcopy(row.source_json))
    return payload


def item_payload(item):
    fields = ('item_id', 'name', 'name_zh', 'description', 'description_zh', 'icon', 'quality',
              'source', 'catalog_type', 'inventory_type', 'slot_key', 'item_class_id',
              'item_subclass_id', 'armor_type', 'weapon_type', 'allowable_class_mask',
              'eligible_specs', 'unique_group', 'effect_refs', 'simc_token', 'enchantment_id', 'metadata')
    return {field: deepcopy(getattr(item, field)) for field in fields} | {'variants': []}


class UpdateSource:
    def discover(self, branch):
        source = CurrentGearCatalogSource()
        product = BRANCHES[branch][0]
        response = source._get('https://wago.tools/builds', params={'product': product})
        rows = inertia(response.text)['builds']['data']
        builds = [row['version'] for row in rows if row.get('product') == product]
        if not builds:
            raise ValueError(f'{branch} 没有可用构建')
        build = max(builds, key=build_number)
        catalog = source._get_json(f'https://www.raidbots.com/static/data/{BRANCHES[branch][1]}/metadata.json')
        # 新增采集字段时，即使上游构建未变，也在下一正常周期补齐一次。
        return {'build': build, 'catalog_build': str(catalog.get('wowBuild') or catalog.get('wow_build') or ''),
                'data_schema': 'loot-specializations-2'}

    def prepare(self, branch, build, run, heartbeat):
        current = WowTalentVersion.objects.filter(key=branch).first()
        old_dir = str(getattr(current, 'source_dir', '') or '').replace('\\', '/')
        slot = 'b' if f'/{branch}/a/' in old_dir else 'a'
        # 两个暂存槽交替使用，准备时不覆盖正在读取的文件，也不按构建累积副本。
        directory = Path(settings.BASE_DIR) / '.cache' / 'wow_data_updates' / branch / slot
        source = BranchCatalogSource(branch, build, heartbeat, directory)
        season = active_season()
        if not season:
            raise ValueError('需先初始化赛季目录；自动更新不会猜测赛季')
        payload = source.build(season_key=season.season_key)
        # 目录更新须覆盖已有的每种装备/强化及装等，不能只更新新增物品。
        by_id = {int(item['item_id']): item for item in payload['items']}
        previous = list(WowItemVariantSnapshot.objects.filter(
            season=season, batch_key=season.gear_batch_key).select_related('item'))
        supplemental = {}
        for row in previous:
            if variant_branch(row) != branch:
                continue
            item = by_id.get(row.item.item_id)
            key = row.variant_key.removeprefix(f'{branch}:')
            if item and any(v.get('key') == key for v in item['variants']):
                continue
            extra = supplemental.setdefault(row.item.item_id, item_payload(row.item))
            value = variant_payload(row)
            value.update(key=key, stats={}, effects=[])
            value['metadata'] = {k: v for k, v in value['metadata'].items()
                                 if k not in ('primary_stat_values', 'primary_stat_amount', 'stats_status',
                                              'effects_status', 'simc_revision', 'game_build')}
            extra['variants'].append(value)
        source._enrich_wowhead(list(supplemental.values()), build)
        for item_id, extra in supplemental.items():
            if item_id in by_id:
                by_id[item_id]['variants'].extend(extra['variants'])
            else:
                by_id[item_id] = extra
        payload['items'] = list(by_id.values())
        for item in payload['items']:
            for value in item['variants']:
                value.setdefault('metadata', {}).update(data_branch=branch, ptr_preview=branch == 'ptr')
                if branch != 'retail':
                    value['key'] = branch + ':' + value['key'].removeprefix(f'{branch}:')
        # 展示更新不依赖执行用的精确构建证据是否完成补采。
        identities, activations = [], []
        fact_warnings = []
        loot_specializations = {}
        try:
            from botend.services.item_loot_specializations import prepare_specializations, eligible_names
            spec_source = WagoJournalSource(build, directory, refresh=True, progress=heartbeat, max_workers=2)
            spec_source.directory = directory / 'loot-specializations'
            loot_specializations = prepare_specializations(branch, spec_source, payload['items'])
            for item in payload['items']:
                if item['item_id'] in loot_specializations:
                    item.update(loot_specializations[item['item_id']])
                    item['eligible_specs'] = eligible_names(item['loot_spec_ids'])
        except MonitorTaskLeaseLost:
            raise
        except Exception as exc:
            fact_warnings.append(f'拾取专精限制补采待重试：{exc}')
        if branch != 'beta':
            try:
                identities, activations = self.facts(source, payload, branch, build, heartbeat)
            except MonitorTaskLeaseLost:
                raise
            except Exception as exc:
                fact_warnings.append(f'执行资料补采待重试：{exc}')
        dump_dir = self.talents(directory, build, heartbeat)
        self.talent_display(source, dump_dir, heartbeat)
        return {'catalog': payload, 'dump_dir': str(dump_dir), 'season_id': season.pk,
                'previous_batch': season.gear_batch_key, 'identities': identities, 'activations': activations,
                'warnings': fact_warnings, 'loot_specializations': loot_specializations}

    @staticmethod
    def facts(source, payload, branch, build, heartbeat):
        from botend.services.wow_item_identity import identity_reference, META_KEY
        from botend.services.wow_item_catalog_import import INVENTORY_SLOTS
        from botend.services.wow_item_effect_activation import ItemEffectActivationCollector, _Collection
        identities, activations = [], []
        ids = [item['item_id'] for item in payload['items']]
        collector = ItemEffectActivationCollector(source)
        for item in WowItemSnapshot.objects.filter(item_id__in=ids):
            heartbeat()
            metadata = item.metadata or {}
            if META_KEY in metadata:
                collection = _Collection(source, collector.limits, item.item_id, build, 'enUS')
                rows = {}
                for table in ('Item', 'ItemSparse'):
                    found, _meta = collection._page(table, 'ID', item.item_id, 1)
                    if len(found) != 1:
                        raise ValueError(f'{item.item_id} 缺少精确构建身份')
                    rows[table] = found[0]
                fact = {'schema_version': 1, 'item_id': item.item_id, 'game_build': build,
                        'inventory_type': rows['Item']['InventoryType'],
                        'slot_key': INVENTORY_SLOTS[rows['Item']['InventoryType']],
                        'item_class_id': rows['Item']['ClassID'], 'item_subclass_id': rows['Item']['SubclassID'],
                        'allowable_class_mask': rows['ItemSparse']['AllowableClass'],
                        'name': rows['ItemSparse']['Display_lang'],
                        'source': {'provider': 'wago_db2', 'evidence': collection.evidence}}
                identity_reference(fact, is_ptr=branch == 'ptr')
                identities.append(fact)
            if 'simc_effect_activation_by_build' in metadata:
                fact = collector.collect(item_id=item.item_id, game_build=build)
                if not fact.get('collection_complete'):
                    raise ValueError(f'{item.item_id} 特效激活事实尚未完整，保留旧版')
                activations.append(fact)
        return identities, activations

    @staticmethod
    def talents(directory, build, heartbeat):
        marker = directory / 'source_build.txt'
        refresh = not marker.is_file() or marker.read_text(encoding='utf-8') != build
        source = WagoJournalSource(build, directory / 'source', progress=heartbeat, refresh=refresh, max_workers=2)
        source.directory = directory / 'source' / 'current'
        output = directory / 'talents' / 'current'
        output.mkdir(parents=True, exist_ok=True)
        tables = [(table, 'zhCN', table) for table in TALENT_TABLES]
        tables += [(table, locale, f'{table}_{locale}')
                   for table in ('SpellName', 'Spell', 'TraitDefinition') for locale in ('enUS', 'zhCN')]
        for table, locale, filename in tables:
            heartbeat()
            rows = source.table(table, locale)
            with (output / f'{filename}.csv').open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        (output / 'manifest.json').write_text(json.dumps(source.manifest, ensure_ascii=False), encoding='utf-8')
        marker.write_text(build, encoding='utf-8')
        return output

    @staticmethod
    def talent_display(source, directory, heartbeat):
        """DB2 图标优先，缺少展示字段时才读取同分支 Wowhead；网络不进入发布事务。"""
        from botend.management.commands.fetch_talent_icons import Command as IconSource
        from botend.management.commands.sync_talent_metadata_from_wowhead import Command as TooltipSource
        def rows(name):
            with (directory / name).open(encoding='utf-8-sig', newline='') as stream:
                return list(csv.DictReader(stream))
        definitions = rows('TraitDefinition.csv')
        spell_ids = {int(row.get(field) or 0) for row in definitions
                     for field in ('SpellID', 'VisibleSpellID', 'OverridesSpellID')} - {0}
        files = {int(row.get('OverrideIcon') or 0) for row in definitions} - {0}
        spell_icons = {}
        for row in rows('SpellMisc.csv'):
            sid = int(row.get('SpellID') or 0)
            fdid = int(row.get('SpellIconFileDataID') or row.get('ActiveIconFileDataID') or 0)
            if sid in spell_ids and fdid:
                spell_icons[sid] = fdid
                files.add(fdid)
        parser = IconSource()
        def fetch_icon(fdid):
            response = source._get(f'https://wago.tools/files?search={fdid}')
            return parser._extract_icon_name(response.text, fdid)
        icons = parser._load_icon_cache(str(directory / 'file_data_icon_cache.csv'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {pool.submit(fetch_icon, fdid): fdid for fdid in files if not icons.get(fdid)}
            for future in as_completed(pending):
                heartbeat()
                try:
                    icons[pending[future]] = future.result()
                except Exception:
                    icons[pending[future]] = ''
        parser._write_icon_cache(str(directory / 'file_data_icon_cache.csv'), icons)
        descriptions = {int(row['ID']): row.get('Description_lang') for row in rows('Spell_zhCN.csv')}
        missing = {sid for sid in spell_ids if not descriptions.get(sid) or not icons.get(spell_icons.get(sid))}
        fallback = {}
        prefix = '' if source.branch == 'retail' else BRANCHES[source.branch][2] + '/'
        def fetch_text(sid):
            payload = source._get_json(f'https://nether.wowhead.com/{prefix}tooltip/spell/{sid}?locale=zhcn')
            return TooltipSource._record_from_payload(payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {pool.submit(fetch_text, sid): sid for sid in missing}
            for future in as_completed(pending):
                heartbeat()
                try:
                    fallback[str(pending[future])] = future.result()
                except Exception:
                    pass
        (directory / 'display_fallback.json').write_text(json.dumps(fallback, ensure_ascii=False), encoding='utf-8')


def validate_catalog(bundle, branch, build):
    payload = bundle['catalog']
    if not payload.get('items'):
        raise ValueError('装备候选为空')
    report = CatalogImporter()._audit_payload(payload)
    if report['blocking_errors']:
        raise ValueError('装备校验失败：' + '; '.join(report['blocking_errors'][:8]))
    missing = []
    for item in payload['items']:
        for row in item['variants']:
            metadata = row.get('metadata') or {}
            if metadata.get('data_branch') != branch:
                raise ValueError('装备数据分支不匹配')
            if not (row.get('stats') or row.get('effects') or metadata.get('primary_stat_values')):
                missing.append(f"{item['item_id']}/{row.get('key')}:属性和特效")
            if (item.get('effect_refs') or item.get('inventory_type') == 12) and not row.get('effects'):
                missing.append(f"{item['item_id']}/{row.get('key')}:特效")
    report['missing_fields'] = missing + bundle.get('warnings', [])
    return report


@transaction.atomic
def prepare_talent_version(bundle, branch, build, heartbeat):
    version = WowTalentVersion.objects.create(
        key=f'{branch}-{build}-{uuid4().hex[:8]}', branch=branch,
        label=f'{dict(retail="正式服", ptr="PTR", beta="Beta")[branch]} {build}',
        major_version='.'.join(build.split('.')[:3]), current_build=build,
        source_dir=bundle['dump_dir'], status='staging', is_active=False,
    )
    output = StringIO()
    call_command('backfill_db2_talent_nodes', dump_dir=bundle['dump_dir'], version_key=version.key,
                 allow_inactive=True, stdout=output)
    heartbeat()
    call_command('repair_talent_metadata_from_db2', dump_dir=bundle['dump_dir'], version_key=version.key,
                 skip_snapshot=True, stdout=output)
    fallback_path = Path(bundle['dump_dir']) / 'display_fallback.json'
    fallback = json.loads(fallback_path.read_text(encoding='utf-8')) if fallback_path.is_file() else {}
    for node in WowTalentNodeMetadata.objects.filter(talent_version=version):
        details = fallback.get(str(node.display_spell_id or node.spell_id), {})
        changed = []
        for field in ('name_zh', 'description_zh', 'icon'):
            if not getattr(node, field) and details.get(field):
                setattr(node, field, details[field])
                changed.append(field)
        if changed:
            node.save(update_fields=changed)
    heartbeat()
    call_command('sync_talent_granted_entries', dump_dir=bundle['dump_dir'], version_key=version.key,
                 build=build, apply=True, stdout=output)
    validate_talents(version)
    # 导入器在隔离事务里完成结构验证，取出候选后回滚；不保存构建副本。
    version._prepared_nodes = list(WowTalentNodeMetadata.objects.filter(talent_version=version))
    version._missing_fields = [f'天赋 {node.node_id}:{field}' for node in version._prepared_nodes
                              if node.tree_type != 'hero_anchor'
                              for field in ('name_zh', 'icon', 'description_zh') if not getattr(node, field)]
    transaction.set_rollback(True)
    return version


def validate_talents(version):
    from botend.constants.wow import CLASS_SPEC_MAP, SPEC_IDENTITY_MAP
    from botend.wow.talents.metadata import AUTHORITATIVE_TALENT_SOURCES
    rows = list(WowTalentNodeMetadata.objects.filter(talent_version=version))
    visible = {(row.class_name, row.spec_name, row.tree_type) for row in rows}
    required = {(cls, spec, tree) for cls, specs in CLASS_SPEC_MAP.items() for spec in specs for tree in ('class', 'spec')}
    if not required.issubset(visible):
        raise ValueError(f'天赋职业/专精覆盖缺失：{sorted(required - visible)}')
    if any(row.source not in AUTHORITATIVE_TALENT_SOURCES for row in rows):
        raise ValueError('天赋结构来源不可用')
    version.refresh_from_db()
    grants = version.granted_entries_json
    if grants.get('build') != version.current_build or set(grants.get('specs', {})) != {str(v) for v in SPEC_IDENTITY_MAP}:
        raise ValueError('天赋赠送节点未覆盖同版本全部专精')
    baseline = WowTalentVersion.objects.filter(branch=version.branch, status='active', is_active=True).order_by('-activated_at', '-id').first()
    if baseline and len(rows) < WowTalentNodeMetadata.objects.filter(talent_version=baseline).count() * .8:
        raise ValueError('天赋节点较当前版本减少超过 20%，需核实上游结构后发布')
    return len(rows)


@transaction.atomic
def publish(bundle, version, state, run, task):
    assert_lease(task)
    state = WowDataUpdateState.objects.select_for_update().get(pk=state.pk)
    season = SeasonMeta.objects.select_for_update().get(pk=bundle['season_id'])
    if season.gear_batch_key != bundle['previous_batch']:
        raise ValueError('准备期间当前目录已变化，下轮重新准备')
    importer = CatalogImporter()
    # 批次和主键保持稳定，已保存配装/强化继续引用原记录。
    batch = season.gear_batch_key
    previous = list(WowItemVariantSnapshot.objects.filter(season=season, batch_key=season.gear_batch_key))
    previous_by_key = {(row.item_id, row.variant_key.removeprefix(f'{run.branch}:')): row
                       for row in previous if variant_branch(row) == run.branch}
    changed_variants = 0
    for payload in bundle['catalog']['items']:
        existing = WowItemSnapshot.objects.select_for_update().filter(item_id=payload['item_id']).first()
        payload = deepcopy(payload)
        # 中央身份/激活事实及人工中文资料不能被另一分支的目录覆盖。
        payload['metadata'] = {**(existing.metadata if existing else {}), **(payload.get('metadata') or {})}
        for key in ('item_identity_by_build', 'simc_effect_activation_by_build', 'localization'):
            if existing and key in (existing.metadata or {}):
                payload['metadata'][key] = deepcopy(existing.metadata[key])
        displays = payload['metadata'].setdefault('branch_display', {})
        base_display = displays.get(run.branch, {})
        if not base_display and existing and run.branch == 'retail':
            base_display = {key: value for key, value in item_payload(existing).items()
                            if key not in ('metadata', 'variants', 'item_id')}
        displays[run.branch] = {**base_display, **{key: deepcopy(value) for key, value in payload.items()
                                if key not in ('metadata', 'variants', 'item_id') and value not in ('', None, [], {})}}
        payload.update(displays[run.branch])
        if existing and run.branch != 'retail':
            # 正式服兼容字段不能被 PTR/Beta 的同 ID 资料覆盖。
            for key in displays[run.branch]:
                if hasattr(existing, key):
                    payload[key] = deepcopy(getattr(existing, key))
        item = importer._upsert_item(payload)
        for variant in payload['variants']:
            defaults = importer._variant_defaults(variant, bundle['catalog'].get('game_build') or run.build)
            defaults['data_branch'] = run.branch
            row = previous_by_key.get((item.pk, variant['key'].removeprefix(f'{run.branch}:')))
            if row:
                for field in ('stats_json', 'effects_json', 'source_json', 'crafting_options', 'compatible_slots'):
                    if not defaults.get(field):
                        defaults[field] = deepcopy(getattr(row, field))
                defaults['metadata'] = {**(row.metadata or {}), **defaults['metadata']}
                fields = [field for field, value in defaults.items() if getattr(row, field) != value]
                if fields:
                    for field in fields:
                        setattr(row, field, defaults[field])
                    row.save(update_fields=[*fields, 'updated_at'])
                    changed_variants += 1
            else:
                if not (defaults['stats_json'] or defaults['effects_json'] or defaults['metadata'].get('primary_stat_values')):
                    continue
                WowItemVariantSnapshot.objects.create(season=season, batch_key=batch, item=item,
                    variant_key=variant['key'], **defaults)
                changed_variants += 1
    # 可变资料只保留分支当前值；任务内已冻结的引用无需回写。
    for field, facts in (('item_identity_by_build', bundle.get('identities', [])),
                         ('simc_effect_activation_by_build', bundle.get('activations', []))):
        for fact in facts:
            from botend.services.wow_item_identity import identity_reference
            from botend.services.wow_item_effect_activation_store import activation_reference
            ref = (identity_reference(fact, is_ptr=run.branch == 'ptr') if field == 'item_identity_by_build'
                   else activation_reference(fact))
            item = WowItemSnapshot.objects.select_for_update().get(item_id=fact['item_id'])
            from botend.services.wow_data_branch import current_fact_store
            store = current_fact_store(item.metadata.get(field) or {})
            store.setdefault('current_by_branch', {})[run.branch] = deepcopy(fact)
            item.metadata[field] = store
            if field == 'item_identity_by_build':
                display = item.metadata.setdefault('branch_display', {}).setdefault(run.branch, {})
                for key in ('inventory_type', 'slot_key', 'item_class_id', 'item_subclass_id',
                            'allowable_class_mask', 'quality', 'name', 'name_zh'):
                    if ref.get(key) not in ('', None):
                        display[key] = ref[key]
            item.save(update_fields=['metadata'])
    target = WowTalentVersion.objects.select_for_update().filter(key=run.branch).first()
    if target is None:
        target = WowTalentVersion.objects.create(key=run.branch, branch=run.branch)
    fields = ('label', 'major_version', 'current_build', 'source_dir', 'granted_entries_json')
    for field in fields:
        setattr(target, field, getattr(version, field))
    target.status, target.is_active, target.activated_at = 'active', True, timezone.now()
    if run.branch == 'retail':
        for flag in DEFAULT_FLAGS:
            setattr(target, flag, True)
    target.save()
    node_key = lambda node: (node.class_name, node.spec_name, node.tree_type, node.node_id)
    old_nodes = {node_key(node): node for node in WowTalentNodeMetadata.objects.filter(talent_version=target)}
    node_fields = [field.name for field in WowTalentNodeMetadata._meta.concrete_fields
                   if field.name not in ('id', 'talent_version', 'last_updated')]
    retained = []
    for node in version._prepared_nodes:
        old = old_nodes.get(node_key(node))
        if old:
            changed = [field for field in node_fields if getattr(old, field) != getattr(node, field)
                       and not (field in ('name', 'name_zh', 'description', 'description_zh', 'icon')
                                and not getattr(node, field))]
            if changed:
                for field in changed:
                    setattr(old, field, getattr(node, field))
                old.last_updated = timezone.now()
                old.save(update_fields=[*changed, 'last_updated'])
            retained.append(old.pk)
        else:
            node.pk, node.talent_version = None, target
            node.save(force_insert=True)
            retained.append(node.pk)
    season.gear_batch_key, season.gear_sync_status, season.gear_synced_at = batch, 'ready', timezone.now()
    if bundle.get('loot_specializations'):
        from botend.services.item_loot_specializations import publish_specializations
        publish_specializations(run.branch, bundle['loot_specializations'])
    if run.branch == 'retail':
        season.game_build = run.build
    report = deepcopy(season.gear_sync_report or {})
    if run.branch == 'retail':
        report['catalog_rules'] = bundle['catalog'].get('rules') or report.get('catalog_rules', {})
    report.setdefault('data_versions', {})[run.branch] = {'build': run.build, 'talent_key': target.key, 'run_id': run.pk}
    season.gear_sync_report = report
    season.save(update_fields=['gear_batch_key', 'gear_sync_status', 'gear_synced_at', 'game_build', 'gear_sync_report', 'updated_at'])
    missing_fields = run.report.get('missing_fields', [])
    state.published_build, state.status, state.error = run.build, ('partial' if missing_fields else 'ready'), ''
    state.revision += 1
    state.published_at, state.projections_pending = timezone.now(), True
    state.report = {'batch_key': batch, 'talent_key': target.key, 'items': len(bundle['catalog']['items']),
                    'changed_variants': changed_variants, 'missing_fields': missing_fields,
                    'source_fingerprint': getattr(run, '_source_fingerprint', run.build)}
    state.save()
    run.status, run.finished_at, run.report = 'published', timezone.now(), state.report
    run.save()
    return state


def refresh_projections(state):
    """队列写入失败仍保留待办，下次无新构建也会重试；不修改模拟历史数值。"""
    from botend.models import SimcBenchmarkPanel
    from botend.services.simc_benchmark_result_snapshot import request_snapshot_refresh
    for panel_id in SimcBenchmarkPanel.objects.filter(is_active=True).values_list('pk', flat=True):
        request_snapshot_refresh(panel_id)
    MonitorTask.objects.filter(name='SpecDetailAggregationMonitor', is_active=True).update(
        last_scan_time=timezone.now() - timedelta(days=365))
    WowDataUpdateState.objects.filter(pk=state.pk, revision=state.revision).update(projections_pending=False)


def sync_game_data(*, monitor_task=None, branches=None, source=None):
    if branches is None:
        from botend.journal_models import JournalState
        journal = JournalState.objects.select_related('active_release').filter(pk='wow-zhCN').first()
        journal_branches = ('ptr',) if journal and journal.active_release and (
            journal.active_release.manifest or {}).get('ptr_overlays') else ()
        branches = tuple(dict.fromkeys(('retail', *WowTalentVersion.objects.filter(is_active=True)
                                       .values_list('branch', flat=True),
                                       *WowItemVariantSnapshot.objects.filter(season__is_active=True)
                                       .values_list('data_branch', flat=True).distinct(), *journal_branches)))
    if not branches or set(branches) - BRANCHES.keys():
        raise ValueError('只支持明确的正式服/PTR/Beta 分支')
    source = source or UpdateSource()
    results, failures = {}, []
    with update_lease(monitor_task) as (task, heartbeat):
        for branch in branches:
            state, _ = WowDataUpdateState.objects.get_or_create(branch=branch)
            run = None
            try:
                if state.projections_pending:
                    refresh_projections(state)
                observation = source.discover(branch)
                build = observation['build'] if isinstance(observation, dict) else observation
                fingerprint = json.dumps(observation, sort_keys=True) if isinstance(observation, dict) else build
                build_number(build)
                state.observed_build, state.checked_at = build, timezone.now()
                state.save(update_fields=['observed_build', 'checked_at'])
                if (state.published_build == build and not state.report.get('missing_fields')
                        and state.report.get('source_fingerprint', state.published_build) == fingerprint):
                    state.status, state.error = 'ready', ''
                    state.save(update_fields=['status', 'error'])
                    results[branch] = {'status': 'unchanged', 'build': state.published_build}
                    continue
                run = WowDataUpdateRun.objects.create(branch=branch, build=build)
                run._source_fingerprint = fingerprint
                state.status, state.error = 'preparing', ''
                state.save(update_fields=['status', 'error'])
                bundle = source.prepare(branch, build, run, heartbeat)
                run.report = validate_catalog(bundle, branch, build)
                run.save(update_fields=['report'])
                version = prepare_talent_version(bundle, branch, build, heartbeat)
                run.report.setdefault('missing_fields', []).extend(getattr(version, '_missing_fields', []))
                heartbeat()
                state = publish(bundle, version, state, run, task)
                refresh_projections(state)
                results[branch] = {'status': 'published', **state.report}
            except Exception as exc:
                if run:
                    run.refresh_from_db()
                # 已发布成功但投影队列失败，绝不能把版本发布状态回滚为失败。
                if run and run.status != 'published':
                    run.status, run.error, run.finished_at = 'failed', str(exc)[:4000], timezone.now()
                    run.save(update_fields=['status', 'error', 'finished_at'])
                with transaction.atomic():
                    assert_lease(task)
                    WowDataUpdateState.objects.filter(pk=state.pk).update(
                        status='projection_pending' if run and run.status == 'published' else 'failed',
                        error=str(exc)[:4000], checked_at=timezone.now())
                failures.append(f'{branch}: {exc}')
            heartbeat()
    if failures:
        raise ValueError('；'.join(failures))
    return results
