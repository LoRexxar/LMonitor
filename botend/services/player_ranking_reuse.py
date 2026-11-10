"""Top20缺失资料的本地榜单回填：批量查询、只缓存引用及缺失复核期限。"""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from botend.models import SpecDungeonRanking, SpecRaidRanking
from botend.raiderio_identity import normalized_name
from botend.services.spec_aggregation_cache import load_state, save_state
from botend.services.spec_stats_snapshot import content_digest


RECHECK = timedelta(hours=24)
MODELS = {'dungeon': SpecDungeonRanking, 'raid': SpecRaidRanking}


def identity(region, realm, name):
    values = [str(value or '').strip() for value in (region, realm, name)]
    return tuple(normalized_name(value) for value in values) if all(values) else None


def ranking_fallbacks(season, players):
    """复用同一来源版本下的结果；大JSON仅加载最终命中的人物行。"""
    groups = defaultdict(dict)
    for player in players:
        key = identity(player.get('region'), player.get('realm'), player.get('name'))
        if key and player.get('_class_name') and player.get('_spec_name'):
            groups[player['_class_name'], player['_spec_name']][key] = player
    result, counts = {}, {'candidates': 0, 'cache_hits': 0, 'queried': 0}
    now = timezone.now().timestamp()
    for (cls, spec), targets in groups.items():
        counts['candidates'] += len(targets)
        queries = {kind: model.objects.filter(season_id=season.pk, class_name=cls, spec_name=spec)
                   for kind, model in MODELS.items()}
        # 每个专精只读两份标量版本；新增、替换、删除及正常更新时间变化均失效。
        revisions = {kind: qs.aggregate(count=Count('pk'), newest=Max('last_updated'), last_id=Max('pk'))
                     for kind, qs in queries.items()}
        fingerprint = content_digest(['player-ranking-reuse-v1', revisions,
                                      getattr(season, 'game_build', ''), getattr(season, 'rio_season', '')])
        scope = f'player-ranking:{cls}:{spec}'
        cached = load_state(season.pk, scope, fingerprint) or {}
        cached = cached if isinstance(cached, dict) else {}
        entries, refs, pending = {}, {}, {}
        for key, player in targets.items():
            digest = content_digest(key)
            entry = cached.get(digest)
            stamp = entry.get('checked_at') if isinstance(entry, dict) else None
            ref = entry.get('ref') if isinstance(entry, dict) else None
            valid_ref = ref is None or (isinstance(ref, list) and len(ref) == 2
                                       and ref[0] in MODELS and type(ref[1]) is int)
            if (type(stamp) in (int, float) and 0 <= now - stamp < RECHECK.total_seconds() and valid_ref):
                entries[digest] = entry
                refs[key] = ref
                counts['cache_hits'] += 1
            else:
                pending[key] = player
        # 缓存只保存行引用；行丢失时本轮重新查，不把旧引用永久当作成功。
        loaded = {}
        for kind, qs in queries.items():
            ids = [ref[1] for ref in refs.values() if ref and ref[0] == kind]
            if ids:
                loaded[kind] = qs.in_bulk(ids)
        for key, ref in list(refs.items()):
            if ref:
                row = loaded.get(ref[0], {}).get(ref[1])
                if row is None or identity(row.region, row.realm, row.character_name) != key:
                    pending[key] = targets[key]
                    counts['cache_hits'] -= 1
                else:
                    result[key, cls, spec] = row
        counts['queried'] += len(pending)
        unresolved = dict(pending)
        for kind, qs in queries.items():
            if not unresolved:
                break
            where = Q()
            for player in unresolved.values():
                where |= Q(region=player['region'], realm=player['realm'], character_name=player['name'])
            selected = {}
            for row in qs.filter(where).exclude(talents_json=[]).order_by('-last_updated', '-pk').values(
                    'pk', 'region', 'realm', 'character_name'):
                key = identity(row['region'], row['realm'], row['character_name'])
                if key in unresolved:
                    selected.setdefault(key, row['pk'])
            hydrated = qs.in_bulk(selected.values()) if selected else {}
            for key, pk in selected.items():
                row = hydrated.get(pk)
                if row is None:
                    continue
                result[key, cls, spec] = row
                entries[content_digest(key)] = {'checked_at': now, 'ref': [kind, pk]}
                unresolved.pop(key)
        for key in unresolved:
            entries[content_digest(key)] = {'checked_at': now, 'ref': None}
        if pending:
            # 每个专精仅保存当前候选的轻量引用，不存装备、天赋或历史人物全集。
            save_state(season.pk, scope, fingerprint, entries)
    return result, counts
