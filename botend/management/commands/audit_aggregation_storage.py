"""只读盘点已知存储目录与聚合发布归属，不打印业务明细。"""
from collections import Counter
import heapq
import json
import os
from pathlib import Path
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def small_json(path):
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('控制文件超限或为链接')
    return json.loads(path.read_text(encoding='utf-8'))


def projection_owners(root):
    """时间只用于分类统计；是否允许删除仍须回收器完整验证。"""
    owners, warnings = {}, []
    if not root.exists():
        return owners, warnings
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        scope = Path(directory)
        if len(scope.relative_to(root).parts) != 3:
            continue
        dirs[:] = []
        current = set()
        for module in ('raid', 'dungeon'):
            path = scope / (module + '.json')
            if not path.exists():
                continue
            try:
                meta = small_json(path).get('_snapshot', {})
                generation = meta.get('generation')
                if generation:
                    current.add(generation)
                    owners[str(scope / 'stats-details' / generation)] = '当前引用'
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                warnings.append({'scope': str(scope), 'reason': str(exc)})
        try:
            manifest = small_json(scope / '.stats-retention.json')
            rows = manifest['records']
            protected = set(current)
            for module in ('raid', 'dungeon'):
                candidates = [r for r in rows if r['meta']['module'] == module
                              and r['meta']['generation'] not in current]
                protected.update(r['meta']['generation'] for r in candidates[-2:])
            for row in rows:
                gen = row['meta']['generation']
                if gen in current:
                    continue
                retired = row['retired_at']
                label = ('回退保护' if gen in protected else '退休超过48小时待校验'
                         if retired is not None and retired < time.time() - 172800 else '宽限内历史')
                owners[str(scope / 'stats-details' / gen)] = label
        except (OSError, ValueError, TypeError, KeyError) as exc:
            warnings.append({'scope': str(scope), 'reason': '历史清单不可核验：' + str(exc)})
        disabled = scope / '.stats-retention-disabled.json'
        if disabled.exists():
            try:
                warnings.append({'scope': str(scope), 'disabled': small_json(disabled)})
            except (OSError, ValueError):
                warnings.append({'scope': str(scope), 'disabled': '禁用标记不可读取'})
    return owners, warnings


def inventory(root, owners, max_files):
    totals, groups, largest = Counter(), Counter(), []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                totals['skipped_links'] += 1
                continue
            if totals['files'] >= max_files:
                return dict(root=str(root), totals=dict(totals), partial=True,
                            groups=groups.most_common(30), largest=sorted(largest, reverse=True))
            try:
                size = path.stat().st_size
            except OSError:
                totals['errors'] += 1
                continue
            totals['files'] += 1
            totals['bytes'] += size
            category = owners.get(str(path.parent))
            if category is None and path.parent.parent.name == 'stats-details':
                category = '未登记历史或失败发布'
            groups[category or '/'.join(path.relative_to(root).parts[:2])] += size
            item = (size, str(path))
            if len(largest) < 10:
                heapq.heappush(largest, item)
            elif size > largest[0][0]:
                heapq.heapreplace(largest, item)
    return dict(root=str(root), totals=dict(totals), partial=False,
                groups=groups.most_common(30), largest=sorted(largest, reverse=True))


class Command(BaseCommand):
    help = '只读盘点聚合、私有缓存及来源文件体积；不会读取或输出角色敏感内容。'

    def add_arguments(self, parser):
        parser.add_argument('--max-files', type=int, default=200000)
        parser.add_argument('--detail', help='额外分析一个统计详情JSON的字段体积和无损去重收益')

    def handle(self, *args, **options):
        if not 1 <= options['max_files'] <= 1000000:
            raise CommandError('文件数上限必须在1至1000000之间')
        base = Path(settings.BASE_DIR)
        media = Path(getattr(settings, 'MEDIA_ROOT', '') or base / 'media')
        published = media / 'aggregated'
        owners, warnings = projection_owners(published)
        from botend.services.spec_aggregation_cache import cache_root
        roots = list(dict.fromkeys([published, cache_root(), media, base / 'var',
                                   base / '.cache', base / 'logs', base / 'botend/data', base / 'tmp']))
        result = {'说明': '目录之间可能包含，不能把各根目录直接相加；历史分类不等于删除授权。',
                  'warnings': warnings, 'roots': [inventory(root, owners, options['max_files']) for root in roots]}
        if options['detail']:
            from botend.services.spec_stats_retention import _safe_path
            from botend.services.stats_storage_codec import pack_detail, unpack_detail
            path = Path(options['detail']).resolve()
            _safe_path(path)
            if not path.is_relative_to(published.resolve()) or path.stat().st_size > 256 * 1024 * 1024:
                raise CommandError('详情必须来自聚合目录，且小于256MiB')
            data = json.loads(path.read_text(encoding='utf-8'))['data']
            data = unpack_detail(data)
            def size(value):
                return sum(len(part.encode('utf-8')) for part in json.JSONEncoder(
                    ensure_ascii=False, separators=(',', ':')).iterencode(value))
            compact = pack_detail(data)
            result['detail'] = dict(path=str(path), fields=sorted(
                ((key, size(value)) for key, value in data.items()), key=lambda item: -item[1]),
                original_bytes=size(data), compact_bytes=size(compact),
                roundtrip_equal=unpack_detail(compact) == data)
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
