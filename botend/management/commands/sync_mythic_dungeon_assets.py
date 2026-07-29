import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from botend.interface.ossupload import ossUploadObject
from botend.models import (
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonEnemy,
    MythicDungeonFloor,
    MythicDungeonSpell,
)
from botend.services.article_image_service import _get_configured_proxies


IMAGE_EXTENSIONS = {'.gif', '.jpeg', '.jpg', '.png', '.webp'}
MAX_REMOTE_IMAGE_BYTES = 12 * 1024 * 1024
WOWHEAD_ASSET_HOSTS = {
    'wow.zamimg.com',
    'wowhead.com',
    'www.wowhead.com',
}


class AssetUnavailableError(RuntimeError):
    pass


class Command(BaseCommand):
    help = '将大秘境规划器地图、怪物图片和技能图片归档到阿里云 OSS。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--version-key',
            default='',
            help='目标数据版本；默认使用当前启用版本。',
        )
        parser.add_argument(
            '--prefix',
            default='mythic-planner',
            help='OSS 对象目录前缀，默认 mythic-planner。',
        )
        parser.add_argument(
            '--cache-dir',
            default='',
            help='远程图片本地缓存目录；默认 .cache/mythic_planner_assets。',
        )
        parser.add_argument(
            '--workers',
            type=int,
            default=6,
            help='下载和上传并发数，默认 6。',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='限制本次实际处理的资源数；默认不限制。',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='只统计和展示对象键，不下载、上传或写数据库。',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='按记录的原始来源重新下载，并覆盖同一个固定 OSS 对象键。',
        )

    def handle(self, *args, **options):
        version = self._resolve_version(options.get('version_key'))
        base_prefix = self._clean_prefix(options.get('prefix'))
        cache_root = self._resolve_cache_root(options.get('cache_dir'))
        oss_base_url = str(
            (getattr(settings, 'OSS_CONFIG', {}) or {}).get('base_url') or ''
        ).strip()
        if not oss_base_url:
            raise CommandError('OSS_CONFIG.base_url 未配置，不能归档规划器资源。')

        version_prefix = f'{base_prefix}/versions/{self._safe_segment(version.key)}'
        jobs, stats = self._build_jobs(
            version=version,
            base_prefix=base_prefix,
            version_prefix=version_prefix,
            oss_base_url=oss_base_url,
            force=bool(options.get('force')),
        )
        limit = max(0, int(options.get('limit') or 0))
        if limit:
            jobs = jobs[:limit]
        workers = max(1, min(16, int(options.get('workers') or 1)))

        self.stdout.write(f'数据版本: {version.key}')
        self.stdout.write(f'OSS 版本前缀: {version_prefix}')
        self.stdout.write(
            '远程图片代理: '
            + (
                '使用项目代理配置'
                if _get_configured_proxies()
                else '未显式配置，遵循系统环境或直连'
            )
        )
        self.stdout.write(
            '待归档: 地图 {floors}、公共技能 {spells}、怪物 {enemies}、'
            '关系技能 {abilities}；已在 OSS {already_oss}、无图片 {empty}、'
            '本地源缺失 {missing_local}'.format(**stats)
        )
        if options.get('dry_run'):
            for job in jobs[:30]:
                self.stdout.write(
                    f"DRY-RUN {job['kind']} {job['source']} -> {job['object_key']}"
                )
            if len(jobs) > 30:
                self.stdout.write(f'... 还有 {len(jobs) - 30} 个资源')
            return

        completed = []
        unavailable = []
        failed = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_jobs = {
                executor.submit(self._process_job, job, cache_root): job
                for job in jobs
            }
            for index, future in enumerate(as_completed(future_jobs), start=1):
                job = future_jobs[future]
                try:
                    public_url = future.result()
                except AssetUnavailableError as exc:
                    unavailable.append((job, str(exc)))
                    self.stderr.write(
                        f"上游无图片: {job['kind']} {job['source']}：{exc}"
                    )
                except Exception as exc:
                    failed.append((job, str(exc)))
                    self.stderr.write(
                        f"归档失败: {job['kind']} {job['source']}：{exc}"
                    )
                else:
                    completed.append((job, public_url))
                if index % 50 == 0 or index == len(future_jobs):
                    self.stdout.write(
                        f'处理进度: {index}/{len(future_jobs)}，'
                        f'成功 {len(completed)}，无图 {len(unavailable)}，'
                        f'失败 {len(failed)}'
                    )

        updated = self._write_results(
            version,
            completed,
            unavailable,
            version_prefix,
            oss_base_url,
        )
        self.stdout.write(self.style.SUCCESS(
            '资源归档完成: 地图 {floors}、公共技能 {spells}、怪物 {enemies}、'
            '关系技能 {abilities}、上游无图 {unavailable}。'.format(**updated)
        ))
        if failed:
            raise CommandError(
                f'有 {len(failed)} 个资源归档失败；成功项已写入，可直接重跑补齐。'
            )

    def _build_jobs(
        self,
        *,
        version,
        base_prefix,
        version_prefix,
        oss_base_url,
        force,
    ):
        jobs = []
        stats = {
            'floors': 0,
            'spells': 0,
            'enemies': 0,
            'abilities': 0,
            'already_oss': 0,
            'empty': 0,
            'missing_local': 0,
        }

        floors = MythicDungeonFloor.objects.filter(
            dungeon__data_version=version,
            is_active=True,
        ).select_related('dungeon').order_by('dungeon__key', 'key')
        for floor in floors:
            current_url = str(floor.background_url or '').strip()
            if self._is_oss_url(current_url, oss_base_url) and not force:
                stats['already_oss'] += 1
                continue
            source_url = current_url
            if self._is_oss_url(current_url, oss_base_url):
                source_url = str(
                    (floor.metadata or {}).get('asset_source_url') or ''
                ).strip()
            local_path = self._local_static_path(source_url)
            if not local_path or not local_path.is_file():
                stats['missing_local'] += 1
                continue
            extension = self._image_extension(local_path.name, '.webp')
            jobs.append({
                'kind': 'floor',
                'instance': floor,
                'source': str(local_path),
                'local_path': local_path,
                'source_url': source_url,
                'object_key': (
                    f'{version_prefix}/maps/'
                    f'{self._safe_segment(floor.dungeon.key)}/'
                    f'{self._safe_segment(floor.key)}{extension}'
                ),
            })
            stats['floors'] += 1

        spells = MythicDungeonSpell.objects.filter(
            data_version=version,
            is_active=True,
        ).order_by('spell_id')
        for spell in spells:
            current_url = str(spell.icon_url or '').strip()
            if (spell.metadata or {}).get('asset_unavailable') and not force:
                stats['empty'] += 1
                continue
            source_url = current_url
            if not source_url or self._is_oss_url(source_url, oss_base_url):
                icon_name = str(spell.icon_name or '').strip().lower()
                if icon_name:
                    source_url = (
                        'https://wow.zamimg.com/images/wow/icons/large/'
                        f'{icon_name}.jpg'
                    )
            if not self._is_remote_url(source_url):
                stats['empty'] += 1
                continue
            extension = self._image_extension(source_url, '.jpg')
            icon_key = self._safe_segment(spell.icon_name or spell.spell_id)
            object_key = self._remote_object_key(
                base_prefix,
                source_url,
                fallback=f'spells/{icon_key}{extension}',
            )
            if (
                not force
                and self._is_oss_object_url(
                    current_url,
                    oss_base_url,
                    object_key,
                )
            ):
                stats['already_oss'] += 1
                continue
            jobs.append({
                'kind': 'spell',
                'instance': spell,
                'source': source_url,
                'source_url': source_url,
                'cache_path': Path('spells') / f'{icon_key}{extension}',
                'refresh_download': force,
                'object_key': object_key,
            })
            stats['spells'] += 1

        enemies = MythicDungeonEnemy.objects.filter(
            dungeon__data_version=version,
            is_active=True,
        ).select_related('dungeon').order_by('dungeon__key', 'key')
        for enemy in enemies:
            source_url = str(enemy.icon_url or '').strip()
            if self._is_oss_url(source_url, oss_base_url) and not force:
                stats['already_oss'] += 1
                continue
            if self._is_oss_url(source_url, oss_base_url):
                source_url = str(
                    (enemy.metadata or {}).get('asset_source_url') or ''
                ).strip()
            if not self._is_remote_url(source_url):
                stats['empty'] += 1
                continue
            extension = self._image_extension(source_url, '.jpg')
            jobs.append({
                'kind': 'enemy',
                'instance': enemy,
                'source': source_url,
                'source_url': source_url,
                'cache_path': (
                    Path('enemies')
                    / self._safe_segment(enemy.dungeon.key)
                    / f'{self._safe_segment(enemy.key)}{extension}'
                ),
                'refresh_download': force,
                'object_key': (
                    self._remote_object_key(
                        base_prefix,
                        source_url,
                        fallback=(
                            f'enemies/{self._safe_segment(enemy.dungeon.key)}/'
                            f'{self._safe_segment(enemy.key)}{extension}'
                        ),
                    )
                ),
            })
            stats['enemies'] += 1

        abilities = MythicDungeonAbility.objects.filter(
            enemy__dungeon__data_version=version,
            is_active=True,
        ).exclude(icon_url='').select_related(
            'enemy__dungeon',
        ).order_by('enemy__dungeon__key', 'enemy__key', 'spell_id')
        for ability in abilities:
            source_url = str(ability.icon_url or '').strip()
            if self._is_oss_url(source_url, oss_base_url) and not force:
                stats['already_oss'] += 1
                continue
            if self._is_oss_url(source_url, oss_base_url):
                source_url = str(
                    (ability.metadata or {}).get('asset_source_url') or ''
                ).strip()
            if not self._is_remote_url(source_url):
                stats['empty'] += 1
                continue
            extension = self._image_extension(source_url, '.jpg')
            relative_name = (
                f'{self._safe_segment(ability.enemy.dungeon.key)}-'
                f'{self._safe_segment(ability.enemy.key)}-'
                f'{ability.spell_id}{extension}'
            )
            jobs.append({
                'kind': 'ability',
                'instance': ability,
                'source': source_url,
                'source_url': source_url,
                'cache_path': Path('abilities') / relative_name,
                'refresh_download': force,
                'object_key': self._remote_object_key(
                    base_prefix,
                    source_url,
                    fallback=f'abilities/{relative_name}',
                ),
            })
            stats['abilities'] += 1
        return self._deduplicate_jobs(jobs), stats

    @staticmethod
    def _deduplicate_jobs(jobs):
        deduplicated = {}
        for job in jobs:
            key = (job['kind'], job['object_key'])
            current = deduplicated.get(key)
            if current is None:
                current = dict(job)
                current['instances'] = [current.pop('instance')]
                deduplicated[key] = current
                continue
            current['instances'].append(job['instance'])
        return list(deduplicated.values())

    @staticmethod
    def _process_job(job, cache_root):
        local_path = job.get('local_path')
        if local_path is None:
            local_path = cache_root / job['cache_path']
            Command._download_image(
                job['source_url'],
                local_path,
                refresh=bool(job.get('refresh_download')),
            )
        last_error = None
        for attempt in range(3):
            try:
                public_url = ossUploadObject(
                    str(local_path),
                    object_key=job['object_key'],
                )
                if public_url:
                    return public_url
                last_error = RuntimeError('OSS 未返回公开地址')
            except Exception as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise last_error or RuntimeError('OSS 上传失败')

    @staticmethod
    def _download_image(source_url, target, *, refresh=False):
        if target.is_file() and target.stat().st_size > 0 and not refresh:
            return target
        proxies = _get_configured_proxies()
        response = None
        last_error = None
        for attempt in range(3):
            try:
                response = requests.get(
                    source_url,
                    timeout=45,
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Accept': (
                            'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'
                        ),
                    },
                    proxies=proxies,
                )
                if response.status_code == 404:
                    raise AssetUnavailableError('上游返回 404')
                response.raise_for_status()
                break
            except AssetUnavailableError:
                raise
            except requests.RequestException as exc:
                if 'Missing dependencies for SOCKS support' in str(exc):
                    raise RuntimeError(
                        'SOCKS 代理需要 PySocks：'
                        'python -m pip install PySocks==1.7.1'
                    ) from exc
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if response is None or last_error is not None and not response.ok:
            raise last_error or RuntimeError('远程图片下载失败')
        content_type = str(response.headers.get('Content-Type') or '').lower()
        if content_type and not content_type.startswith('image/'):
            raise RuntimeError(f'远程响应不是图片: {content_type}')
        content = response.content
        if not content:
            raise RuntimeError('远程图片内容为空')
        if len(content) > MAX_REMOTE_IMAGE_BYTES:
            raise RuntimeError(
                f'远程图片超过 {MAX_REMOTE_IMAGE_BYTES // 1024 // 1024} MiB'
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    @staticmethod
    def _write_results(
        version,
        completed,
        unavailable,
        object_prefix,
        oss_base_url,
    ):
        grouped = {
            'floor': [],
            'spell': [],
            'enemy': [],
            'ability': [],
        }
        spell_urls = {}
        now = timezone.now()
        for job, public_url in completed:
            for instance in job['instances']:
                metadata = dict(instance.metadata or {})
                metadata.update({
                    'asset_source_url': job.get('source_url') or '',
                    'asset_oss_object_key': job['object_key'],
                    'asset_synced_at': now.isoformat(),
                })
                instance.metadata = metadata
                instance.updated_at = now
                if job['kind'] == 'floor':
                    instance.background_url = public_url
                else:
                    instance.icon_url = public_url
                grouped[job['kind']].append(instance)
                if job['kind'] == 'spell':
                    spell_urls[int(instance.spell_id)] = public_url

        unavailable_count = 0
        for job, reason in unavailable:
            for instance in job['instances']:
                if job['kind'] == 'floor':
                    continue
                metadata = dict(instance.metadata or {})
                metadata.update({
                    'asset_source_url': job.get('source_url') or '',
                    'asset_unavailable': True,
                    'asset_unavailable_reason': reason,
                    'asset_synced_at': now.isoformat(),
                })
                instance.icon_url = ''
                instance.metadata = metadata
                instance.updated_at = now
                grouped[job['kind']].append(instance)
                unavailable_count += 1

        if grouped['floor']:
            MythicDungeonFloor.objects.bulk_update(
                grouped['floor'],
                ['background_url', 'metadata', 'updated_at'],
                batch_size=200,
            )
        for kind, model in (
            ('spell', MythicDungeonSpell),
            ('enemy', MythicDungeonEnemy),
            ('ability', MythicDungeonAbility),
        ):
            if grouped[kind]:
                model.objects.bulk_update(
                    grouped[kind],
                    ['icon_url', 'metadata', 'updated_at'],
                    batch_size=500,
                )

        inherited_abilities = []
        if spell_urls:
            for ability in MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                spell_id__in=spell_urls,
                is_active=True,
            ):
                current_url = str(ability.icon_url or '').strip()
                inherited_spell_id = int(
                    (ability.metadata or {}).get(
                        'asset_inherited_from_spell',
                    ) or 0
                )
                should_inherit = (
                    not current_url
                    or (
                        '/mythic-planner/sources/wow.zamimg.com/'
                        in current_url
                    )
                    or inherited_spell_id == int(ability.spell_id)
                )
                if not should_inherit:
                    continue
                ability.icon_url = spell_urls[int(ability.spell_id)]
                metadata = dict(ability.metadata or {})
                metadata['asset_inherited_from_spell'] = int(ability.spell_id)
                metadata['asset_synced_at'] = now.isoformat()
                ability.metadata = metadata
                ability.updated_at = now
                inherited_abilities.append(ability)
            if inherited_abilities:
                MythicDungeonAbility.objects.bulk_update(
                    inherited_abilities,
                    ['icon_url', 'metadata', 'updated_at'],
                    batch_size=500,
                )

        version_metadata = dict(version.metadata or {})
        version_metadata['asset_snapshot'] = {
            'provider': 'aliyun-oss',
            'base_url': oss_base_url.rstrip('/') + '/',
            'object_prefix': object_prefix,
            'synced_at': now.isoformat(),
            'updated': {
                'floors': len(grouped['floor']),
                'spells': len(grouped['spell']),
                'enemies': len(grouped['enemy']),
                'abilities': len(grouped['ability']) + len(inherited_abilities),
                'unavailable': unavailable_count,
            },
        }
        version.metadata = version_metadata
        version.save(update_fields=['metadata', 'updated_at'])
        return {
            'floors': len(grouped['floor']),
            'spells': len(grouped['spell']),
            'enemies': len(grouped['enemy']),
            'abilities': len(grouped['ability']) + len(inherited_abilities),
            'unavailable': unavailable_count,
        }

    @staticmethod
    def _resolve_version(version_key):
        queryset = MythicDungeonDataVersion.objects.all()
        if str(version_key or '').strip():
            version = queryset.filter(key=str(version_key).strip()).first()
            if not version:
                raise CommandError(f'找不到 MDT 数据版本: {version_key}')
            return version
        version = queryset.filter(is_active=True).order_by('-imported_at').first()
        if not version:
            raise CommandError('找不到当前启用的 MDT 数据版本。')
        return version

    @staticmethod
    def _clean_prefix(value):
        prefix = str(value or '').strip().strip('/')
        if not prefix or '..' in prefix.split('/'):
            raise CommandError('--prefix 不能为空且不能包含上级目录。')
        return '/'.join(
            Command._safe_segment(part)
            for part in prefix.split('/')
            if part
        )

    @staticmethod
    def _safe_segment(value):
        cleaned = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(value or '')).strip('-._')
        return cleaned or 'asset'

    @staticmethod
    def _image_extension(value, default):
        suffix = Path(urlsplit(str(value or '')).path).suffix.lower()
        return suffix if suffix in IMAGE_EXTENSIONS else default

    @staticmethod
    def _remote_object_key(base_prefix, source_url, *, fallback):
        parsed = urlsplit(str(source_url or '').strip())
        if parsed.scheme in {'http', 'https'} and parsed.netloc:
            path_parts = [
                Command._safe_segment(unquote(part))
                for part in parsed.path.split('/')
                if part
            ]
            if path_parts:
                if parsed.netloc.lower() in WOWHEAD_ASSET_HOSTS:
                    return '/'.join(['wowhead', *path_parts])
                return '/'.join([
                    base_prefix,
                    'sources',
                    Command._safe_segment(parsed.netloc.lower()),
                    *path_parts,
                ])
        return f"{base_prefix}/sources/{str(fallback or '').lstrip('/')}"

    @staticmethod
    def _is_remote_url(value):
        parsed = urlsplit(str(value or '').strip())
        return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)

    @staticmethod
    def _is_oss_url(value, oss_base_url):
        parsed = urlsplit(str(value or '').strip())
        oss = urlsplit(str(oss_base_url or '').strip())
        return (
            parsed.scheme in {'http', 'https'}
            and bool(parsed.netloc)
            and parsed.netloc.lower() == oss.netloc.lower()
        )

    @staticmethod
    def _is_oss_object_url(value, oss_base_url, object_key):
        parsed = urlsplit(str(value or '').strip())
        oss = urlsplit(str(oss_base_url or '').strip())
        expected_path = '/'.join([
            str(oss.path or '').strip('/'),
            str(object_key or '').strip('/'),
        ]).strip('/')
        return (
            parsed.scheme in {'http', 'https'}
            and bool(parsed.netloc)
            and parsed.netloc.lower() == oss.netloc.lower()
            and unquote(parsed.path).strip('/') == expected_path
        )

    @staticmethod
    def _local_static_path(value):
        parsed = urlsplit(str(value or '').strip())
        path = unquote(parsed.path or '')
        static_url = str(getattr(settings, 'STATIC_URL', '/static/') or '/static/')
        static_prefix = urlsplit(static_url).path.rstrip('/') + '/'
        if not path.startswith(static_prefix):
            return None
        static_root = (Path(settings.BASE_DIR) / 'static').resolve()
        target = (static_root / path[len(static_prefix):]).resolve()
        if target != static_root and static_root not in target.parents:
            return None
        return target

    @staticmethod
    def _resolve_cache_root(configured):
        if str(configured or '').strip():
            return Path(str(configured).strip()).expanduser().resolve()
        return (Path(settings.BASE_DIR) / '.cache' / 'mythic_planner_assets').resolve()
