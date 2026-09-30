"""MDT 正式发布的每日检查、离线转换、资源预发布和事务升级。"""

import json
import logging
import re
import time
import zipfile
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path, PurePosixPath

import requests
from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from botend.models import (
    MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost,
    MythicDungeonDataVersion, MythicDungeonEnemy, MythicDungeonPoi,
    MythicDungeonAbility, MythicDungeonSpawn, MythicDungeonSyncRun, WowSpellSnapshot,
)
from botend.plugin_sync import claim_monitor_task, release_monitor_task_lease, renew_monitor_task_lease
from botend.management.commands.sync_mythic_dungeon_tools import load_payload_seed
from botend.mythic_planner.mdt_converter import (
    SOURCE_TAG, UI_ASSET_FILES, build_payload, compose_maps, write_payload,
)
from botend.mythic_planner.icon_assets import build_wowhead_icon_url
from botend.mythic_planner.wowhead_tooltips import fetch_wowhead_tooltip
from botend.services.article_image_service import _get_request_proxies


TASK_NAME = 'MythicDungeonToolsMonitor'
TASK_TYPE = 36  # 只追加监控类型；历史索引不可重排。
SOURCE_REPO = 'https://github.com/Nnoggie/MythicDungeonTools'
API_ROOT = 'https://api.github.com/repos/Nnoggie/MythicDungeonTools'
VERSION_RE = re.compile(r'^(?:v)?(\d+)\.(\d+)\.(\d+)$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
logger = logging.getLogger(__name__)


def version_number(tag):
    match = VERSION_RE.fullmatch(str(tag or ''))
    return tuple(map(int, match.groups())) if match else None


def get_mdt_monitor_task():
    task, _ = MonitorTask.objects.get_or_create(name=TASK_NAME, defaults={
        'type': TASK_TYPE, 'target': SOURCE_REPO, 'is_active': True,
        'wait_time': 86400, 'last_scan_time': timezone.now() - timedelta(days=2),
        'notes': '每日检查 MDT 正式版本；校验和地图上传成功后原位升级，失败保留当前数据。',
    })
    return task


def assert_lease(task):
    """在发布事务内锁住任务及租约，阻止过期执行者提交。"""
    MonitorTask.objects.select_for_update().get(pk=task.pk)
    if not MonitorTaskLease.objects.select_for_update().filter(
        task_id=task.pk, owner=task._monitor_task_lease_owner,
        expires_at__gt=timezone.now(),
    ).exists():
        raise MonitorTaskLeaseLost('MDT 更新执行锁已失效，停止发布。')


@contextmanager
def update_lease(task=None):
    task = task or get_mdt_monitor_task()
    if task.name != TASK_NAME:
        raise ValueError('不是 MDT 更新任务。')
    manual = not getattr(task, '_monitor_task_lease_owner', None)
    if manual:
        task = claim_monitor_task(task.pk)
        if task is None:
            raise MonitorTaskLeaseLost('已有 MDT 更新任务正在执行。')

    def heartbeat():
        if not renew_monitor_task_lease(task.pk, task._monitor_task_lease_owner):
            raise MonitorTaskLeaseLost('MDT 更新执行锁已失效。')

    try:
        heartbeat()
        yield task, heartbeat
    finally:
        if manual:
            release_monitor_task_lease(task.pk, task._monitor_task_lease_owner)


class UpstreamClient:
    """只访问固定官方仓库，读取数据文件而不执行下载的代码。"""

    def __init__(self, request_client=None):
        self.session = requests.Session()
        self.session.proxies.update(_get_request_proxies(request_client) or {})
        self.session.headers.update({'User-Agent': 'LMonitor-MDT-Updater', 'Accept': 'application/vnd.github+json'})
        self.session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',), respect_retry_after_header=False,
        )))

    def json(self, path, *, optional=False):
        with self.session.get(API_ROOT + path, timeout=(15, 45)) as response:
            if optional and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    def latest_release(self):
        refs = self.json('/git/matching-refs/tags/')
        if not isinstance(refs, list):
            raise ValueError('上游标签响应不是列表。')
        tags = [row['ref'].removeprefix('refs/tags/') for row in refs if row.get('ref', '').startswith('refs/tags/')]
        tags = sorted((tag for tag in tags if version_number(tag)), key=version_number, reverse=True)
        for tag in tags[:20]:
            release = self.json(f'/releases/tags/{tag}', optional=True)
            if not release or release.get('draft') or release.get('prerelease'):
                continue
            if release.get('tag_name') != tag:
                raise ValueError('正式发布与标签不匹配。')
            obj = next(row['object'] for row in refs if row['ref'] == f'refs/tags/{tag}')
            for _ in range(5):
                if obj.get('type') == 'commit':
                    break
                if obj.get('type') != 'tag' or not SHA_RE.fullmatch(obj.get('sha', '')):
                    raise ValueError('上游标签未指向有效提交。')
                obj = self.json(f"/git/tags/{obj['sha']}")['object']
            if obj.get('type') != 'commit' or not SHA_RE.fullmatch(obj.get('sha', '')):
                raise ValueError('无法解析正式版本的固定提交。')
            return {'tag': tag, 'commit': obj['sha']}
        raise ValueError('没有找到可用的 MDT 正式发布。')

    def download(self, release, destination, heartbeat):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://codeload.github.com/Nnoggie/MythicDungeonTools/zip/{release['commit']}"
        total = 0
        last_beat = time.monotonic()
        with self.session.get(url, stream=True, timeout=(15, 45)) as response:
            response.raise_for_status()
            with destination.open('wb') as output:
                for block in response.iter_content(1024 * 1024):
                    total += len(block)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError('上游压缩包超过允许的体积。')
                    output.write(block)
                    if time.monotonic() - last_beat > 30:
                        heartbeat()
                        last_beat = time.monotonic()
        heartbeat()
        return extract_source(destination, destination.parent / 'source')


def extract_source(archive, destination):
    """只解压转换器需要的数据，拒绝越界路径、链接和异常大文件。"""
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 20000 or sum(entry.file_size for entry in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError('上游解压规模异常。')
        roots = set()
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            if not parts or '\\' in entry.orig_filename or ':' in entry.orig_filename or '..' in parts or PurePosixPath(entry.filename).is_absolute():
                raise ValueError('上游压缩包包含不安全路径。')
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('上游压缩包包含符号链接。')
            roots.add(parts[0])
            path = PurePosixPath(*parts[1:])
            name = path.as_posix()
            selected = name in {'LICENSE', 'Locales/enUS.lua', 'Locales/zhCN.lua', 'Modules/DungeonSelect.lua', 'Midnight/load_midnight.xml'}
            selected |= path.parent.as_posix() == 'Midnight' and path.suffix == '.lua'
            selected |= name.startswith('Midnight/Textures/') and path.suffix == '.png'
            selected |= name in {f'Textures/{file}' for file in UI_ASSET_FILES}
            if entry.is_dir() or not selected:
                continue
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError('上游路径超出工作目录。')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(entry))
        if len(roots) != 1:
            raise ValueError('上游压缩包应只有一个根目录。')
    return destination


def builtin_package_path():
    return Path(settings.BASE_DIR) / 'botend/data/mythic_planner' / f'mdt_{SOURCE_TAG.replace(".", "_")}.json'


def payload_counts(payload):
    dungeons = payload.get('dungeons') or []
    return {
        'dungeons': len(dungeons),
        'enemies': sum(len(d['enemies']) for d in dungeons),
        'spawns': sum(len(e['spawns']) for d in dungeons for e in d['enemies']),
        'abilities': sum(len(e['abilities']) for d in dungeons for e in d['enemies']),
        'spells': len({a['spell_id'] for d in dungeons for e in d['enemies'] for a in e['abilities']}),
        'pois': sum(len(f['pois']) for d in dungeons for f in d['floors']),
    }


def database_counts(version):
    return {
        'dungeons': version.dungeons.filter(is_active=True).count(),
        'enemies': MythicDungeonEnemy.objects.filter(dungeon__data_version=version, is_active=True).count(),
        'spawns': MythicDungeonSpawn.objects.filter(enemy__dungeon__data_version=version, is_active=True).count(),
        'abilities': MythicDungeonAbility.objects.filter(enemy__dungeon__data_version=version, is_active=True).count(),
        'spells': version.spells.filter(is_active=True).count(),
        'pois': MythicDungeonPoi.objects.filter(floor__dungeon__data_version=version, is_active=True).count(),
    }


def validate_candidate(payload, release, baseline=None):
    metadata = payload['data_version']['metadata']
    if metadata['source_tag'] != release['tag'] or metadata['source_commit'] != release['commit']:
        raise ValueError('转换结果与已核实的上游提交不一致。')
    counts = payload_counts(payload)
    if not all(counts.values()):
        raise ValueError('上游数据不完整，不能覆盖当前版本。')
    keys = [d['key'] for d in payload['dungeons']]
    indexes = [d['external_index'] for d in payload['dungeons']]
    if len(set(keys)) != len(keys) or len(set(indexes)) != len(indexes):
        raise ValueError('地下城标识重复。')
    for dungeon in payload['dungeons']:
        floor_keys = {floor['key'] for floor in dungeon['floors']}
        if not floor_keys or not dungeon['enemies'] or dungeon['total_enemy_forces'] <= 0:
            raise ValueError(f"{dungeon['key']} 缺少地图、怪物或进度。")
        for enemy in dungeon['enemies']:
            for spawn in enemy['spawns']:
                if spawn['floor_key'] not in floor_keys or not (0 <= spawn['x'] <= 100 and 0 <= spawn['y'] <= 100):
                    raise ValueError(f"{dungeon['key']} 存在无效刷新点。")
            for ability in enemy['abilities']:
                if not ability['name_zh'] or ability['name_zh'].startswith('技能 #'):
                    raise ValueError(f"技能 {ability['spell_id']} 缺少中文资料。")
        for floor in dungeon['floors']:
            for poi in floor['pois']:
                if poi['type'] == 'genericItem':
                    tip = poi['metadata'].get('tooltip') or {}
                    if not poi['label'] or not poi['icon_url'] or not tip.get('description_zh'):
                        raise ValueError(f"{dungeon['key']} 的交互标记缺少图标或中文说明。")
    if baseline:
        new = {d['key']: d for d in payload['dungeons']}
        for previous in baseline['dungeons']:
            current = new.get(previous['key'])
            if not current:
                raise ValueError(f"上游移除了地下城 {previous['key']}，需要人工核实赛季切换。")
            for field in ('enemies', 'spawns'):
                before = len(previous['enemies']) if field == 'enemies' else sum(len(e['spawns']) for e in previous['enemies'])
                after = len(current['enemies']) if field == 'enemies' else sum(len(e['spawns']) for e in current['enemies'])
                if after < before * .75:
                    raise ValueError(f"{previous['key']} 的 {field} 减少超过 25%，停止自动发布。")
    return counts


def seed_metadata(version):
    snapshots, _, icons, metadata = load_payload_seed(builtin_package_path())
    if version:
        for spell in version.spells.filter(is_active=True):
            snapshots[spell.spell_id] = {
                **snapshots.get(spell.spell_id, {}),
                'name': spell.name, 'name_zh': spell.name_zh,
                'description': spell.description_zh, 'icon_url': spell.icon_url,
            }
        for poi in MythicDungeonPoi.objects.filter(floor__dungeon__data_version=version, is_active=True):
            info = ((poi.metadata or {}).get('source') or {}).get('info') or {}
            if info.get('spellId'):
                spell_id = int(info['spellId'])
                snapshots[spell_id] = {
                    **snapshots.get(spell_id, {}), **((poi.metadata or {}).get('tooltip') or {}),
                    'name_zh': poi.label, 'icon_url': poi.icon_url,
                }
        icons.update({(row['dungeon__key'], row['key']): row['icon_url'] for row in MythicDungeonEnemy.objects.filter(
            dungeon__data_version=version, is_active=True,
        ).exclude(icon_url='').values('dungeon__key', 'key', 'icon_url')})
        metadata.update(version.metadata or {})
    return snapshots, icons, metadata


def prepare_candidate(source, release, version, heartbeat):
    """沿用已审核补充关系；保持既有转换器对人工补充表的优先级。"""
    baseline = json.loads(builtin_package_path().read_text(encoding='utf-8'))
    kwargs = {'source_tag': release['tag'], 'source_commit': release['commit']}
    raw = build_payload(source, **kwargs)
    current_enemies = {(d['key'], e['npc_id']): e for d in raw['dungeons'] for e in d['enemies']}
    supplement = {}
    for dungeon in baseline['dungeons']:
        for enemy in dungeon['enemies']:
            spells = [a['spell_id'] for a in enemy['abilities'] if a['metadata'].get('source') == 'LMonitorAbilitySupplement']
            current = current_enemies.get((dungeon['key'], enemy['npc_id']))
            if spells and current:
                supplement.setdefault(dungeon['key'], {'enemies': {}})['enemies'][str(enemy['npc_id'])] = spells
    override = Path(source) / 'LMonitor/ability_overrides.json'
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({
        'schema_version': 1, 'target': {'source_tag': release['tag'], 'source_commit': release['commit']},
        'dungeons': supplement, 'notes_zh': '继承已发布的人工审核技能补充关系；保持既有补充表的覆盖规则。',
    }, ensure_ascii=False), encoding='utf-8')
    snapshots, icons, metadata = seed_metadata(version)
    known_spell_ids = set(snapshots)
    raw = build_payload(source, **kwargs)
    poi_ids = {int(info['spellId']) for d in raw['dungeons'] for f in d['floors'] for p in f['pois']
               for info in [p['metadata']['source'].get('info') or {}] if info.get('spellId')}
    spell_ids = {a['spell_id'] for d in raw['dungeons'] for e in d['enemies'] for a in e['abilities']} | poi_ids
    # 只查询正式服同一构建；不把 PTR 快照静默混入发布。
    build = (metadata.get('spell_snapshot') or {}).get('snapshot_build')
    rows = WowSpellSnapshot.objects.filter(branch='wow', locale='zhCN', spell_id__in=spell_ids)
    if build:
        rows = rows.filter(snapshot_build=build)
    for row in rows.order_by('updated_at'):
        current = snapshots.setdefault(row.spell_id, {})
        for name, value in [('name', row.name), ('name_zh', row.name_zh), ('description', row.description)]:
            if not current.get(name) and value and '$' not in value:
                current[name] = value
    for spell_id in sorted(spell_ids):
        current = snapshots.setdefault(spell_id, {})
        # 已审核的战斗事件可能没有图标，不因这一可选字段重新请求外部资料。
        fields = ('name', 'name_zh') if spell_id in known_spell_ids else ('name', 'name_zh', 'icon_url')
        if spell_id in poi_ids:
            fields += ('icon_url', 'description_zh', 'description')
        if all(current.get(field) for field in fields):
            continue
        heartbeat()
        logger.info('MDT 补全技能资料：%s', spell_id)
        zh = fetch_wowhead_tooltip(spell_id, locale=4, include_buff=spell_id in poi_ids) or {}
        en = fetch_wowhead_tooltip(spell_id, locale=0, include_buff=spell_id in poi_ids) or {}
        if not zh.get('name') or not en.get('name'):
            raise ValueError(f'新技能 {spell_id} 未取得完整名称，保留当前版本。')
        current.update({
            'name': en['name'], 'name_zh': zh['name'],
            'description': en.get('description', '') if spell_id in poi_ids else zh.get('description', ''),
            'description_zh': zh.get('description', ''),
            'icon_url': current.get('icon_url') or build_wowhead_icon_url(zh.get('icon_name') or en.get('icon_name')),
            'icon_name': zh.get('icon_name') or en.get('icon_name'), 'source': 'wowhead_tooltip',
            'data_env': 1, 'difficulty_id': 8, 'locales': ['enUS', 'zhCN'],
        })
    payload = build_payload(source, spell_snapshots=snapshots, enemy_icon_urls=icons, **kwargs)
    # 旧版统计和 OSS 前缀不能冒充本次同步结果。
    payload['data_version']['metadata']['inherited_spell_snapshot'] = metadata.get('spell_snapshot', {})
    heartbeat()
    validate_candidate(payload, release, baseline)
    return payload


def publish_maps(payload, source, workdir, heartbeat):
    from botend.interface.ossupload import ossUploadObject

    maps = compose_maps(source, Path(workdir) / 'maps', progress=heartbeat)
    by_key = {}
    for item in maps:
        heartbeat()
        path = Path(item['output'])
        object_key = f"mythic-planner/auto/maps/{item['sha256']}.webp"
        url = ossUploadObject(str(path), object_key)
        if not url:
            raise RuntimeError(f"地图上传失败：{item['dungeon_key']}，保留当前版本。")
        by_key[(item['dungeon_key'], item['floor_index'])] = url
    for dungeon in payload['dungeons']:
        for floor in dungeon['floors']:
            floor['background_url'] = by_key[(dungeon['key'], floor['floor_index'])]


def active_version():
    versions = list(MythicDungeonDataVersion.objects.filter(is_active=True).order_by('pk'))
    if len(versions) > 1:
        raise ValueError('同时存在多个活动 MDT 版本，停止自动升级。')
    version = versions[0] if versions else None
    if version and version.source_name != 'MythicDungeonTools':
        raise ValueError('当前使用自定义数据版本，自动更新不会覆盖。')
    return version


def import_candidate(payload, package_path, version, task, heartbeat):
    """复用原位导入并将导入后复核放在提交前，任何失败整体回滚。"""
    target_key = payload['data_version']['key']
    original_key = version.key if version else ''
    expected = payload_counts(payload)
    options = {'file_path': str(package_path), 'activate': True, 'replace': True, 'stdout': StringIO()}
    if version:
        options['upgrade_from_version'] = original_key
    for dry_run in (True, False):
        heartbeat()
        with transaction.atomic():
            assert_lease(task)
            list(MythicDungeonDataVersion.objects.select_for_update().order_by('pk'))
            current = active_version()
            if (current.pk if current else None, current.key if current else '') != (version.pk if version else None, original_key):
                raise ValueError('准备期间活动版本已经改变，停止本次升级。')
            if current and current.imported_at != version.imported_at:
                raise ValueError('准备期间当前数据已被重新导入，停止本次升级。')
            call_command('import_mythic_dungeon_data', **options)
            imported = MythicDungeonDataVersion.objects.get(key=target_key, is_active=True)
            if database_counts(imported) != expected:
                raise ValueError('导入后计数与候选数据不一致，已回滚。')
            assert_lease(task)
            if dry_run:
                transaction.set_rollback(True)
    return expected


def sync_latest_mdt(*, monitor_task=None, request_client=None, check_only=False):
    with update_lease(monitor_task) as (task, heartbeat):
        MythicDungeonSyncRun.objects.filter(status='running').update(
            status='failed', error='上次进程中断或执行锁已过期，后台开始新的检查。', finished_at=timezone.now(),
        )
        run = MythicDungeonSyncRun.objects.create()
        client = None
        try:
            client = UpstreamClient(request_client)
            version = active_version()
            run.source_version = version.key if version else ''
            release = client.latest_release()
            run.target_version = release['tag']
            run.source_commit = release['commit']
            run.save()
            metadata = version.metadata if version else {}
            current_tag = (metadata or {}).get('source_tag', '')
            current_number = version_number(current_tag)
            latest_number = version_number(release['tag'])
            if version and current_number is None:
                raise ValueError('当前 MDT 版本缺少可比较的正式版本号，停止自动升级。')
            if current_number and latest_number <= current_number:
                if latest_number == current_number and (metadata or {}).get('source_commit') != release['commit']:
                    raise ValueError('同一上游标签的提交发生变化，拒绝静默覆盖。')
                run.status = 'unchanged'
            elif check_only:
                run.status = 'available'
            else:
                logger.info('MDT 批次 %s：准备升级 %s → %s', run.pk, current_tag, release['tag'])
                workdir = Path(settings.BASE_DIR) / '.cache/mdt-auto-update' / str(run.pk)
                workdir.mkdir(parents=True, exist_ok=True)
                source = client.download(release, workdir / 'source.zip', heartbeat)
                payload = prepare_candidate(source, release, version, heartbeat)
                # 对当前数据库的规模再校验，避免只与内置版本比较。
                if version:
                    baseline = {'dungeons': [
                        {'key': d.key, 'enemies': [{'spawns': list(e.spawns.filter(is_active=True).values_list('pk', flat=True))}
                                                 for e in d.enemies.filter(is_active=True)]}
                        for d in version.dungeons.filter(is_active=True)
                    ]}
                    validate_candidate(payload, release, baseline)
                publish_maps(payload, source, workdir, heartbeat)
                logger.info('MDT 批次 %s：地图准备完成，开始事务导入', run.pk)
                package_path = write_payload(payload, workdir / 'package.json')
                run.summary = import_candidate(payload, package_path, version, task, heartbeat)
                run.status = 'updated'
            labels = {'unchanged': '已是最新版本', 'available': '发现新版本（仅检查）', 'updated': '更新完成'}
            displayed_tag = current_tag if run.status == 'unchanged' else run.target_version
            task.flag = f"MDT {displayed_tag} · {labels[run.status]} · 批次 {run.pk}"
            task.save(update_fields=['flag'])
        except Exception as exc:
            run.status = 'failed'
            run.error = str(exc)[:4000]
            try:
                task.flag = f'MDT 更新失败 · 批次 {run.pk} · {run.error[:1000]}'
                task.save(update_fields=['flag'])
            except MonitorTaskLeaseLost:
                pass
            raise
        finally:
            run.finished_at = timezone.now()
            run.save()
            if client is not None:
                client.session.close()
        return run
