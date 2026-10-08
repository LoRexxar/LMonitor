"""冒险手册公开目录、首领详情及其共用数据接口。"""
from urllib.parse import urlencode

from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views import View

from botend.journal_models import JournalState
from botend.services.journal_service import ROLE_FLAGS, SLOTS
from botend.services.journal_text import integer
from botend.services.journal_loot import class_matches, equipment_type, specialization_options, enrich_loot_specializations
from botend.services.journal_tooltip import cached_tooltip
from botend.services.wow_item_display import load_item_display_metadata
from botend.services.season_keys import canonical_season_key
from botend.services.gear_builder import active_season
from botend.services import journal_snapshot as journal_files


CLASSES = [(1, '战士'), (2, '圣骑士'), (3, '猎人'), (4, '潜行者'), (5, '牧师'), (6, '死亡骑士'),
           (7, '萨满祭司'), (8, '法师'), (9, '术士'), (10, '武僧'), (11, '德鲁伊'), (12, '恶魔猎手'), (13, '唤魔师')]
KINDS = {'dungeon': '地下城', 'raid': '团队副本', 'world': '世界首领', 'affix': '大秘境机制'}
_UNSET = object()


def current_release():
    state = JournalState.objects.select_related('active_release').filter(pk='wow-zhCN').first()
    return state.active_release if state else None


def _version_label(build):
    return '.'.join(str(build or '').split('.')[:3])


def instance_source(release, instance_id, *, season=_UNSET):
    manifest = release.manifest or {}
    overlay = (manifest.get('ptr_overlays') or {}).get(str(instance_id))
    display = (manifest.get('display_overrides') or {}).get(str(instance_id))
    if display:
        if season is _UNSET:
            season = active_season()
        if season and canonical_season_key(display.get('season_key')) == canonical_season_key(season.season_key):
            return {'key': 'current', 'label': '本赛季',
                    'build': str((overlay or {}).get('source_build') or release.build.split('+ptr-', 1)[0]),
                    'text_build': display['localization_build']}
    if overlay:
        build = str(overlay.get('source_build') or '')
        return {'key': 'ptr', 'label': f'PTR {_version_label(build)}', 'build': build}
    build = str(manifest.get('retail_build') or release.build.split('+ptr-', 1)[0])
    return {'key': 'retail', 'label': f'正式服 {_version_label(build)}', 'build': build}


def _present_tooltip(data, source):
    """赛季展示标签与保留的数值来源分开，避免把旧 PTR 构建伪装成正式服。"""
    if data and source['key'] == 'current':
        return {**data, 'source': 'LMonitor 装备目录'}
    return data


def catalog_data(request):
    query = request.GET.get('q', '').strip()[:100]
    kind = request.GET.get('kind', '')
    tier = integer(request.GET.get('tier'))
    result = {'release': None, 'instances': [], 'tiers': [], 'q': query, 'kind': kind, 'tier': tier}
    try:
        index = journal_files.read_index()
        catalog = journal_files.read_catalog(index)
    except journal_files.JournalSnapshotUnavailable:
        return {**result, 'snapshot': {'state': 'building'}}
    result.update(release=journal_files.release_summary(index), tiers=catalog['tiers'], season_label=catalog['season_label'])
    if 'tier' not in request.GET:
        tier = next((row['id'] for row in result['tiers'] if row['order'] == 9000), 0)
        result['tier'] = tier
    rows = []
    filters = []
    for entry in catalog['instances']:
        visible = (kind not in KINDS or entry['kind'] == kind) and (not tier or tier in entry['tier_ids']) and (
            not query or any(query.casefold() in name.casefold() for name in [entry['name'], *entry['_boss_names']]))
        row = {key: value for key, value in entry.items() if not key.startswith('_')}
        rows.append({**row, 'visible': visible})
        if visible:
            result['instances'].append(row)
        filters.append({'id': row['id'], 'kind': row['kind'], 'tier_ids': row['tier_ids'],
                        'search_names': [row['name'], *entry['_boss_names']]})
    result.update(instance_catalog=rows, catalog_filter_data={'rows': filters}, snapshot={'state': 'ready'})
    return result


def detail_data(request, instance_id, *, for_html=False):
    index = journal_files.read_index()
    data = journal_files.read_instance(index, instance_id)
    instance = data['instance']
    available = instance['difficulty_ids']
    difficulty = integer(request.GET.get('difficulty'), available[0] if available else 0)
    if difficulty not in available:
        raise Http404('此副本不支持所选难度')
    role = request.GET.get('role', '')
    role = role if role in ('tank', 'healer', 'dps') else ''
    selected = integer(request.GET.get('boss'))
    bosses, boss = journal_files.select_boss(data, difficulty, selected)
    class_id = integer(request.GET.get('class'))
    if class_id not in {cid for cid, _ in CLASSES}:
        class_id = 0
    specs = specialization_options(class_id)
    spec_id = integer(request.GET.get('spec'))
    if spec_id not in {spec['id'] for spec in specs}:
        spec_id = 0
    keep = {key: request.GET[key] for key in ('slot', 'item_type', 'loot_q', 'loot_boss') if key in request.GET}
    keep.update({'class': class_id or '', 'spec': spec_id or '', 'difficulty': difficulty, 'role': role})
    source = instance['source']
    result = {'release': journal_files.release_summary(index), 'source': source, 'instance': instance,
              'bosses': [{'id': row['id'], 'name': row['name'], 'url': '?' + urlencode({**keep, 'tab': 'skills', 'boss': row['id']})}
                         for row in bosses], 'boss': None, 'difficulty': difficulty, 'role': role,
              'difficulties': [row for row in index['difficulties'] if row['id'] in available],
              'roles': [{'id': key, 'name': label} for _, key, label in ROLE_FLAGS],
              'slots': [], 'classes': [{'id': cid, 'name': label} for cid, label in CLASSES],
              'slot': request.GET.get('slot', ''), 'class_id': class_id, 'spec_id': spec_id, 'specs': specs,
              'item_type': request.GET.get('item_type', ''), 'loot_boss': integer(request.GET.get('loot_boss')),
              'loot_q': request.GET.get('loot_q', '').strip()[:100], 'loot': [], 'loot_total': 0, 'item_types': [],
              'loot_url': '?' + urlencode({**keep, 'tab': 'loot'}),
              'tab': 'skills' if request.GET.get('tab') == 'skills' or ('tab' not in request.GET and selected) else 'loot'}
    if instance['kind'] == 'world':
        result['difficulties'] = [{'id': difficulty, 'name': '世界首领'}]
    if not boss:
        return result
    if result['loot_boss'] and result['loot_boss'] not in {row['id'] for row in bosses}:
        result['loot_boss'] = 0
    from botend.services.journal_loot_snapshot import read_loot_projection
    projection = read_loot_projection(index['release']['id'], instance_id, difficulty, source,
                                      journal_version=data['loot_versions'][str(difficulty)])
    result.update({key: projection.get(key, []) for key in ('slots', 'item_types')})
    result['loot_snapshot'] = projection['snapshot']
    drops = projection.get('loot', [])
    result['loot_total'] = projection.get('loot_total', 0)
    for row in drops:
        row['visible'] = loot_matches(row, result)
    filtered = [row for row in drops if row['visible']]
    result['loot_catalog'] = drops
    result['loot_filter_data'] = {
        'specs': {str(cid): specialization_options(cid) for cid, _ in CLASSES},
        'rows': [{key: row[key] for key in ('item_id', 'slot', 'item_type', 'sources', 'search_names', 'filter_classes', 'filter_specs')}
                 for row in drops],
    }
    if result['slot'] and integer(result['slot']) not in {row['id'] for row in result['slots']}:
        result['slots'].append({'id': integer(result['slot']), 'name': SLOTS.get(integer(result['slot']), '其他')})
    result['loot'] = filtered
    if not for_html or result['tab'] == 'skills':
        skills = journal_files.read_skills(index, data, boss, difficulty)
        result['boss'] = journal_files.filter_skills(skills, role)
        result['skill_filter_data'] = {key: skills[key] for key in ('boss', 'sections', 'roles')}
    else:
        # 掉落 HTML 只需要首领身份；不加载任何战斗技能正文。
        result['boss'] = {'id': boss['id'], 'name': boss['name']}
    result['boss'].update(loot=[row for row in filtered if boss['id'] in {owner['id'] for owner in row['sources']}],
                          loot_total=sum(boss['id'] in {owner['id'] for owner in row['sources']} for row in drops))
    return result


def build_loot_projection(release, instance_id, bosses, difficulty, source):
    """后台构建完整副本掉落，保留旧的资格、分支和数值展示规则。"""
    by_item = {}
    for owner in bosses:
        for drop in owner.payload['loot']:
            if difficulty not in drop['difficulty_ids']:
                continue
            type_id, type_name = equipment_type(drop)
            row = by_item.setdefault(drop['item_id'], {**drop, 'item_type': type_id, 'type_name': type_name, 'sources': []})
            if owner.journal_id not in {s['id'] for s in row['sources']}:
                row['sources'].append({'id': owner.journal_id, 'name': owner.name})
            row['condition_id'] = row['condition_id'] or drop['condition_id']
            row['display_season_id'] = row['display_season_id'] or drop['display_season_id']
    drops = enrich_loot_specializations(list(by_item.values()), source['key'], fallback_branch=(
        'ptr' if str(instance_id) in (release.manifest or {}).get('ptr_overlays', {}) else 'retail'))
    # 基础身份独立于 tooltip 完整度；只投影副本掉落，不改发布快照。
    display_metadata = load_item_display_metadata(by_item)
    filtered = []
    seen = set()
    for row in drops:
        row['search_names'] = [row['name']]
        display = display_metadata[row['item_id']]
        row['name_localized'] = bool(display['name_zh']) or bool(row.get('name_localized'))
        if display['name_zh']:
            row['name'] = display['name_zh']
        row['icon_url'] = display['icon_url']
        row['search_names'].append(row['name'])
        row['filter_classes'] = [cid for cid, _ in CLASSES if class_matches(row, cid)]
        row['filter_specs'] = [spec['id'] for cid, _ in CLASSES for spec in specialization_options(cid)
                               if class_matches(row, cid, spec['id'])]
        identity = (row['item_id'], row['faction'], row['display_season_id'], row['condition_id'])
        if identity in seen:
            continue
        seen.add(identity)
        filtered.append({**row, 'details': _present_tooltip(cached_tooltip(
            'item', row['item_id'], difficulty, source['build'],
            use_current_catalog=source['key'] == 'current',
            data_branch=source['key'] if source['key'] in ('ptr', 'beta') else '',
        ), source)})
    return {'loot': filtered, 'loot_total': len(drops),
            'slots': [{'id': slot, 'name': SLOTS.get(slot, '其他')} for slot in sorted({d['slot'] for d in drops})],
            'item_types': [{'id': key, 'name': name} for key, name in sorted({(d['item_type'], d['type_name']) for d in drops})]}


def loot_matches(row, result):
    """与浏览器共用已投影的职业资格，避免请求内读取装备目录。"""
    query = result['loot_q']
    return (not result['loot_boss'] or any(s['id'] == result['loot_boss'] for s in row['sources'])) and (
        not result['item_type'] or row['item_type'] == result['item_type']) and (
        result['slot'] == '' or row['slot'] == integer(result['slot'])) and (
        not result['class_id'] or result['class_id'] in row['filter_classes']) and (
        not result['spec_id'] or result['spec_id'] in row['filter_specs']) and (
        not query or query == str(row['item_id']) or any(query.casefold() in name.casefold() for name in row['search_names']))


class PortalAdventureJournalView(View):
    def get(self, request):
        return render(request, 'portal/adventure_journal.html', catalog_data(request))


class PortalAdventureJournalDetailView(View):
    def get(self, request, instance_id):
        try:
            result = detail_data(request, instance_id, for_html=True)
        except journal_files.JournalSnapshotUnavailable:
            return HttpResponse('冒险手册资料暂不可用，请稍后刷新重试。', status=503)
        return render(request, 'portal/adventure_journal_detail.html', result)


class PortalAdventureJournalAPIView(View):
    def get(self, request, instance_id=None):
        try:
            if instance_id and request.GET.get('snapshot_status') == '1':
                from botend.services.journal_loot_snapshot import read_loot_projection
                index = journal_files.read_index()
                data = journal_files.read_instance(index, instance_id)
                available = data['instance']['difficulty_ids']
                difficulty = integer(request.GET.get('difficulty'), available[0] if available else 0)
                if difficulty not in available:
                    raise Http404
                projection = read_loot_projection(index['release']['id'], instance_id, difficulty, data['instance']['source'],
                                                  journal_version=data['loot_versions'][str(difficulty)])
                response = JsonResponse({'snapshot': projection['snapshot']})
                response['Cache-Control'] = 'no-store'
                return response
            result = detail_data(request, instance_id) if instance_id else catalog_data(request)
        except journal_files.JournalSnapshotUnavailable:
            return JsonResponse({'snapshot': {'state': 'unavailable'}, 'message': '手册资料暂不可用，请稍后重试。'}, status=503)
        for key in ('loot_catalog', 'loot_filter_data', 'instance_catalog', 'catalog_filter_data', 'skill_filter_data'):
            result.pop(key, None)
        return JsonResponse(result, json_dumps_params={'ensure_ascii': False})


class PortalAdventureJournalArtView(View):
    def get(self, request, file_id):
        try:
            index = journal_files.read_index()
        except journal_files.JournalSnapshotUnavailable:
            return HttpResponse('手册图片资料暂不可用。', status=503)
        if file_id not in index['art_ids']:
            raise Http404('图片未被当前手册引用')
        from botend.services.journal_media import cached_art
        try:
            path = cached_art(file_id)
        except Exception:
            raise Http404('来源图片暂不可用')
        response = FileResponse(path.open('rb'), content_type='image/webp')
        response['Cache-Control'] = 'public, max-age=604800'
        return response


class PortalAdventureJournalTooltipView(View):
    def get(self, request, instance_id, kind, entry_id):
        if kind not in ('item', 'spell'):
            raise Http404('不支持的详情类型')
        try:
            index = journal_files.read_index()
            data = journal_files.read_instance(index, instance_id)
            available = data['instance']['difficulty_ids']
            difficulty = integer(request.GET.get('difficulty'), available[0] if available else 0)
            if difficulty not in available:
                raise Http404('此副本不支持所选难度')
            owners, boss = journal_files.select_boss(data, difficulty, integer(request.GET.get('boss')))
            if not boss:
                raise Http404('没有首领')
            source = data['instance']['source']
            context = {'difficulty': difficulty, 'release': {'build': source['build']}, 'source': source}
            if kind == 'spell':
                skills = journal_files.read_skills(index, data, boss, difficulty)
                referenced = skills['tooltips'].get(str(entry_id))
                if not referenced:
                    raise Http404('此首领当前难度未引用该技能')
                return JsonResponse({'name': referenced['title'], 'lines': referenced['text'].splitlines(),
                                     'source': 'Wago', 'url': f'https://wago.tools/journal/{instance_id}?build={source.get("text_build") or source["build"]}',
                                     'note': f'{source["label"]}，按当前难度解析；动态效果以游戏内实际状态为准。'},
                                    json_dumps_params={'ensure_ascii': False})
            referenced = data['references'][str(difficulty)].get(str(entry_id))
            if not referenced:
                raise Http404('此副本当前难度未引用该物品')
        except journal_files.JournalSnapshotUnavailable:
            return JsonResponse({'message': '手册详情暂不可用，请稍后重试。'}, status=503)
        from botend.services.journal_tooltip import tooltip
        try:
            return JsonResponse(_present_tooltip(tooltip(
                kind, entry_id, context['difficulty'], context['release']['build'],
                use_current_catalog=source['key'] == 'current',
                data_branch=source['key'] if source['key'] in ('ptr', 'beta') else '',
            ), source), json_dumps_params={'ensure_ascii': False})
        except ValueError:
            item = load_item_display_metadata([entry_id])[entry_id]
            if item['display_name'] == f'#{entry_id}':
                raise Http404('中央物品目录缺少此物品')
            qualities = {0: '粗糙', 1: '普通', 2: '优秀', 3: '精良', 4: '史诗', 5: '传说', 6: '神器', 7: '传家宝'}
            journal_item = item['journal_item']
            lines = [f'{qualities.get(item["quality"], "物品")} · {SLOTS.get(item["inventory_type"], "其他")}']
            bonding = {1: '拾取后绑定', 2: '装备后绑定', 3: '使用后绑定', 4: '任务物品'}.get(journal_item.get('bonding'))
            if bonding:
                lines.append(bonding)
            if journal_item.get('required_level', 0) > 0:
                lines.append(f'需要等级 {journal_item["required_level"]}')
            if item['display_description']:
                lines.extend(item['display_description'].splitlines())
            source_names = [owner['name'] for owner in referenced]
            lines.append('掉落首领：' + '、'.join(source_names))
            status = 'basic' if item['catalog_type'] == 'equipment' else 'not_equipment'
            note = (
                '中央物品目录已确认这是非装备掉落；此物品没有装备属性或装备特效。'
                if status == 'not_equipment' else
                '中央物品目录只有基础事实，尚无匹配此构建的装备属性变体。'
            )
            return JsonResponse({
                'name': item['display_name'],
                'lines': lines,
                'source': 'LMonitor 中央物品目录',
                'url': item['wowhead_url'],
                'icon': item['icon_url'],
                'note': note,
                'status': status,
                'complete': False,
                'item_level': None,
                'stats': [],
                'effects': [],
            }, json_dumps_params={'ensure_ascii': False})
