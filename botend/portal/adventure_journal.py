"""冒险手册公开目录、首领详情及其共用数据接口。"""
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from botend.journal_models import JournalEncounter, JournalInstance, JournalState
from botend.services.journal_service import ROLE_FLAGS, SLOTS
from botend.services.journal_text import integer
from botend.services.journal_loot import class_matches, equipment_type
from botend.services.journal_tooltip import cached_tooltip


CLASSES = [(1, '战士'), (2, '圣骑士'), (3, '猎人'), (4, '潜行者'), (5, '牧师'), (6, '死亡骑士'),
           (7, '萨满祭司'), (8, '法师'), (9, '术士'), (10, '武僧'), (11, '德鲁伊'), (12, '恶魔猎手'), (13, '唤魔师')]
KINDS = {'dungeon': '地下城', 'raid': '团队副本', 'world': '世界首领'}


def current_release():
    state = JournalState.objects.select_related('active_release').filter(pk='wow-zhCN').first()
    return state.active_release if state else None


def catalog_data(request):
    release = current_release()
    query = request.GET.get('q', '').strip()[:100]
    kind = request.GET.get('kind', '')
    tier = integer(request.GET.get('tier'))
    result = {'release': None, 'instances': [], 'tiers': [], 'q': query, 'kind': kind, 'tier': tier}
    if not release:
        return result
    rows = JournalInstance.objects.filter(release=release).annotate(boss_count=Count('encounters')).order_by('-expansion', 'journal_id')
    if query:
        matching = JournalEncounter.objects.filter(instance__release=release, name__icontains=query).values('instance_id')
        rows = rows.filter(Q(name__icontains=query) | Q(id__in=matching))
    if kind in KINDS:
        rows = rows.filter(kind=kind)
    result['release'] = {'id': release.id, 'build': release.build, 'updated': release.completed_at,
                         'counts': {k: release.report.get(k, 0) for k in ('instances', 'encounters', 'loot')}}
    result['tiers'] = sorted(release.manifest['catalog']['tiers'], key=lambda t: -t['order'])
    if 'tier' not in request.GET:
        tier = next((t['id'] for t in result['tiers'] if t['order'] == 9000), 0)
        result['tier'] = tier
    for row in rows:
        if tier and tier not in row.payload.get('tier_ids', []):
            continue
        payload = {**row.payload, 'boss_count': row.boss_count, 'kind_label': KINDS.get(row.kind, '副本'),
                   'url': f'/portal/adventure-journal/{row.journal_id}/'}
        payload['tier_name'] = next((t['name'] for t in result['tiers'] if t['id'] in row.payload['tier_ids'] and t['order'] != 9000), '')
        counts = [n for n in row.payload.get('boss_counts', {}).values() if n] or [row.boss_count]
        payload['boss_count_label'] = str(max(counts)) if min(counts) == max(counts) else f'{min(counts)}–{max(counts)}'
        result['instances'].append(payload)
    return result


def detail_data(request, instance_id):
    release = current_release()
    if not release:
        raise Http404('冒险手册尚未同步')
    instance = get_object_or_404(JournalInstance, release=release, journal_id=instance_id)
    available = instance.payload['difficulty_ids']
    difficulty = integer(request.GET.get('difficulty'), available[0] if available else 0)
    if difficulty not in available:
        raise Http404('此副本不支持所选难度')
    role = request.GET.get('role', '')
    if role not in ('tank', 'healer', 'dps'):
        role = ''
    bosses = list(instance.encounters.all())
    selected = integer(request.GET.get('boss'))
    if selected and not any(b.journal_id == selected for b in bosses):
        raise Http404('此首领不属于所选副本')
    requested = next((b for b in bosses if b.journal_id == selected), None)
    bosses = [b for b in bosses if difficulty in b.payload['difficulty_ids']]
    boss = next((b for b in bosses if b.journal_id == selected), None)
    if boss is None and requested:
        boss = next((b for b in bosses if b.name == requested.name), None)
    boss = boss or (bosses[0] if bosses else None)
    keep = {key: request.GET[key] for key in ('slot', 'class', 'item_type', 'loot_q') if key in request.GET}
    keep.update(difficulty=difficulty, role=role)
    result = {'release': {'id': release.id, 'build': release.build, 'updated': release.completed_at},
              'instance': {**instance.payload, 'kind_label': KINDS.get(instance.kind, '副本')},
              'bosses': [{'id': b.journal_id, 'name': b.name, 'url': '?' + urlencode({**keep, 'tab': 'skills', 'boss': b.journal_id})}
                         for b in bosses], 'boss': None, 'difficulty': difficulty, 'role': role,
              'difficulties': [d for d in release.manifest['catalog']['difficulties'] if d['id'] in available],
              'roles': [{'id': key, 'name': label} for _, key, label in ROLE_FLAGS],
              'slots': [], 'classes': [{'id': cid, 'name': name} for cid, name in CLASSES],
              'slot': request.GET.get('slot', ''), 'class_id': integer(request.GET.get('class')),
              'item_type': request.GET.get('item_type', ''), 'loot_boss': integer(request.GET.get('loot_boss')),
              'loot_q': request.GET.get('loot_q', '').strip()[:100],
              'loot': [], 'loot_total': 0, 'item_types': [],
              'loot_url': '?' + urlencode({**keep, 'tab': 'loot'}),
              'tab': 'skills' if request.GET.get('tab') == 'skills' or ('tab' not in request.GET and selected) else 'loot'}
    if instance.kind == 'world':
        result['difficulties'] = [{'id': difficulty, 'name': '世界首领'}]
    if result['class_id'] not in {cid for cid, _ in CLASSES}:
        result['class_id'] = 0
    if not boss:
        return result
    payload = boss.payload
    sections = []
    role_sections = []
    for section in payload['sections']:
        if difficulty not in section['difficulty_ids']:
            continue
        row = {k: v for k, v in section.items() if k not in ('source_text', 'descriptions', 'dynamic')}
        row['text'] = section['descriptions'].get(str(difficulty), '')
        row['has_dynamic'] = bool(section['dynamic'].get(str(difficulty)))
        row['role_names'] = [label for _, key, label in ROLE_FLAGS if key in row['roles']]
        if section['type'] == 3 and section['roles']:
            if not role or role in section['roles']:
                role_sections.append(row)
        elif section['type'] != 3 and (not role or not section['roles'] or role in section['roles']):
            sections.append(row)
    # 过滤后重建可见层级，避免职责过滤留下不可见的父标题。
    shown = {r['id']: r for r in sections}
    skill_tree = []
    for row in sections:
        row['children'] = []
        if row['parent'] in shown:
            shown[row['parent']]['children'].append(row)
        else:
            skill_tree.append(row)
    # 掉落属于副本，首领选择仅控制战斗指南；独立掉落首领筛选默认包含全部。
    if result['loot_boss'] and result['loot_boss'] not in {b.journal_id for b in bosses}:
        result['loot_boss'] = 0
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
    drops = list(by_item.values())
    result['loot_total'] = len(drops)
    result['item_types'] = [{'id': key, 'name': name} for key, name in sorted({(d['item_type'], d['type_name']) for d in drops})]
    result['slots'] = [{'id': slot, 'name': SLOTS.get(slot, '其他')} for slot in sorted({d['slot'] for d in drops})]
    if result['slot'] and integer(result['slot']) not in {s['id'] for s in result['slots']}:
        result['slots'].append({'id': integer(result['slot']), 'name': SLOTS.get(integer(result['slot']), '其他')})
    filtered = []
    seen = set()
    for row in drops:
        if result['loot_boss'] and result['loot_boss'] not in {s['id'] for s in row['sources']}:
            continue
        if result['item_type'] and row['item_type'] != result['item_type']:
            continue
        if result['slot'] != '' and row['slot'] != integer(result['slot']):
            continue
        if not class_matches(row, result['class_id']):
            continue
        if result['loot_q'] and result['loot_q'].casefold() not in row['name'].casefold() and result['loot_q'] != str(row['item_id']):
            continue
        identity = (row['item_id'], row['faction'], row['display_season_id'], row['condition_id'])
        if identity in seen:
            continue
        seen.add(identity)
        filtered.append({**row, 'details': cached_tooltip('item', row['item_id'], difficulty, release.build)})
    result['loot'] = filtered
    overview = next((s['descriptions'].get(str(difficulty), '') for s in payload['sections']
                     if s['type'] == 3 and not s['roles'] and difficulty in s['difficulty_ids']), '')
    result['boss'] = {k: payload[k] for k in ('id', 'name', 'description', 'creatures')}
    result['boss']['faction'] = payload.get('faction', 'both')
    result['boss'].update({'overview': overview, 'skills': skill_tree, 'roles': role_sections,
                           'loot': [r for r in filtered if boss.journal_id in {s['id'] for s in r['sources']}],
                           'loot_total': sum(boss.journal_id in {s['id'] for s in r['sources']} for r in drops), 'skill_total': len(sections),
                           'has_dynamic': any(s['has_dynamic'] for s in sections),
                           'available': difficulty in payload['difficulty_ids']})
    return result


class PortalAdventureJournalView(View):
    def get(self, request):
        return render(request, 'portal/adventure_journal.html', catalog_data(request))


class PortalAdventureJournalDetailView(View):
    def get(self, request, instance_id):
        return render(request, 'portal/adventure_journal_detail.html', detail_data(request, instance_id))


class PortalAdventureJournalAPIView(View):
    def get(self, request, instance_id=None):
        result = detail_data(request, instance_id) if instance_id else catalog_data(request)
        return JsonResponse(result, json_dumps_params={'ensure_ascii': False})


class PortalAdventureJournalArtView(View):
    def get(self, request, file_id):
        release = current_release()
        if not release or file_id not in release.manifest['catalog'].get('art_ids', []):
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
        # 详情补充只验证引用，避免每件装备请求都重新投影整张副本掉落表。
        release = current_release()
        if not release:
            raise Http404('冒险手册尚未同步')
        instance = get_object_or_404(JournalInstance, release=release, journal_id=instance_id)
        available = instance.payload['difficulty_ids']
        difficulty = integer(request.GET.get('difficulty'), available[0] if available else 0)
        if difficulty not in available:
            raise Http404('此副本不支持所选难度')
        owners = list(instance.encounters.all())
        selected = integer(request.GET.get('boss'))
        requested = next((owner for owner in owners if owner.journal_id == selected), None)
        if selected and requested is None:
            raise Http404('此首领不属于所选副本')
        owners = [owner for owner in owners if difficulty in owner.payload['difficulty_ids']]
        boss = next((owner for owner in owners if owner.journal_id == selected), None)
        if boss is None and requested:
            boss = next((owner for owner in owners if owner.name == requested.name), None)
        boss = boss or next(iter(owners), None)
        if boss is None:
            raise Http404('没有首领')
        context = {'difficulty': difficulty, 'release': {'build': release.build}}
        if kind == 'item':
            rows = [row for owner in owners for row in owner.payload['loot']]
        else:
            rows = boss.payload['sections']
        field = 'item_id' if kind == 'item' else 'spell_id'
        referenced = next((row for row in rows if row[field] == entry_id and context['difficulty'] in row['difficulty_ids']), None)
        if not referenced:
            raise Http404('此首领当前难度未引用该物品或技能')
        if kind == 'spell':
            return JsonResponse({'name': referenced['title'],
                                 'lines': referenced['descriptions'].get(str(context['difficulty']), '').splitlines(),
                                 'source': 'Wago', 'url': f'https://wago.tools/journal/{instance_id}?build={context["release"]["build"]}',
                                 'note': f'正式服 {context["release"]["build"]}，按当前难度解析；动态效果以游戏内实际状态为准。'},
                                json_dumps_params={'ensure_ascii': False})
        from botend.services.journal_tooltip import tooltip
        try:
            return JsonResponse(tooltip(kind, entry_id, context['difficulty'], context['release']['build']), json_dumps_params={'ensure_ascii': False})
        except Exception:
            qualities = {0: '粗糙', 1: '普通', 2: '优秀', 3: '精良', 4: '史诗', 5: '传说', 6: '神器', 7: '传家宝'}
            stats = {3: '敏捷', 4: '力量', 5: '智力', 7: '耐力', 32: '爆击', 36: '急速', 40: '全能', 49: '精通',
                     61: '速度', 62: '吸血', 63: '闪避', 71: '力量／敏捷／智力', 72: '力量／敏捷', 73: '敏捷／智力', 74: '力量／智力'}
            lines = [f'{qualities.get(referenced["quality"], "物品")} · {referenced["slot_name"]}']
            bonding = {1: '拾取后绑定', 2: '装备后绑定', 3: '使用后绑定', 4: '任务物品'}.get(referenced.get('bonding'))
            if bonding:
                lines.append(bonding)
            if referenced.get('required_level', 0) > 0:
                lines.append(f'需要等级 {referenced["required_level"]}')
            names = [stats[value] for value in referenced.get('stat_types', []) if value in stats]
            if names:
                lines.append('属性类型：' + '、'.join(names))
            if referenced.get('description'):
                lines.append(referenced['description'])
            source_names = [owner.name for owner in owners if any(
                row['item_id'] == entry_id and context['difficulty'] in row['difficulty_ids'] for row in owner.payload['loot'])]
            lines.append('掉落首领：' + '、'.join(source_names))
            return JsonResponse({'name': referenced['name'], 'lines': lines, 'source': 'Wago',
                                 'url': f'https://wago.tools/db2/ItemSparse?build={context["release"]["build"]}&locale=zhCN&filter[ID]={entry_id}',
                                 'note': '以上为已同步的物品资料。补充属性来源暂不可访问；装备等级、属性数值与触发效果请以游戏内物品为准。'},
                                json_dumps_params={'ensure_ascii': False})
