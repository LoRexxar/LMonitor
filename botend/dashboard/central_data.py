"""中央装备与天赋资料的后台只读聚合查询，不生成第二份资料。"""
import re
from urllib.parse import urlsplit
from django.core.paginator import Paginator
from django.db.models import BigIntegerField, Case, CharField, Count, Exists, F, IntegerField, Max, Min, OuterRef, Q, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, NullIf
from django.http import JsonResponse
from django.views import View

from botend.constants.wow import CLASS_CN, CLASS_SPEC_MAP, SPEC_CN, QUALITY_CN
from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot, WowSpellSnapshot, WowTalentNodeMetadata
from botend.services.gear_builder import SLOT_LABELS
from botend.services.wow_data_branch import branch_item, item_cdn_icon
from botend.templatetags.wow_tags import wow_icon_oss_url
from botend.services.wow_item_effects import resolve_effects
from botend.services.wow_item_display import _native_journal_variant
from botend.services.wow_spell_display import SpellDisplayReader, node_spell_id
from botend.wow.branches import spell_branch
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.versioning import TalentVersionResolver

TREES = {'class': '职业天赋', 'spec': '专精天赋', 'hero': '英雄天赋', 'hero_anchor': '英雄树入口'}
TRACKS = {'explorer': '探索者', 'adventurer': '冒险者', 'veteran': '老兵', 'champion': '勇士', 'hero': '英雄', 'myth': '神话'}
SLOTS = {**SLOT_LABELS, 'finger': '戒指', 'trinket': '饰品', 'weapon': '武器'}
CJK = '[\u3400-\u9fff]'


def number(params, key, default=None, maximum=2147483647):
    raw = params.get(key)
    if raw in (None, ''):
        return default
    if not str(raw).isdecimal() or not 0 <= int(raw) <= maximum:
        raise ValueError(f'{key} 参数无效')
    return int(raw)


def options(mapping):
    return [{'value': str(key), 'label': value} for key, value in mapping.items()]


def icon_payload(icon):
    name = str(icon or '')
    url = f'https://wow.zamimg.com/images/wow/icons/large/{name}.jpg' if re.fullmatch(r'[A-Za-z0-9_-]+', name) else ''
    return {'icon': name, 'icon_url': url, 'icon_source': 'Wowhead 图标 CDN；图标名称取自中央资料'}


def page_rows(queryset, params):
    size = number(params, 'page_size', 25, 100)
    if size not in (10, 25, 50, 100):
        raise ValueError('每页数量仅支持 10、25、50、100')
    page = Paginator(queryset, size).get_page(number(params, 'page', 1, 100000) or 1)
    return list(page.object_list), {'page': page.number, 'pages': page.paginator.num_pages,
                                 'page_size': size, 'total': page.paginator.count}


def localized_filter(queryset, field, value):
    if value == 'translated':
        return queryset.filter(**{field + '__regex': CJK})
    if value == 'missing':
        return queryset.exclude(**{field + '__regex': CJK})
    if value not in ('', 'all'):
        raise ValueError('中文筛选参数无效')
    return queryset


class CentralDataAPI(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'database.central-data'
    http_method_names = ['get', 'head', 'options']

    def dispatch(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
        except ValueError as exc:
            response = JsonResponse({'success': False, 'error': str(exc)}, status=400)
        response['Cache-Control'] = 'private, no-store'
        return response

    def get(self, request):
        params = request.GET
        branch = params.get('branch', 'retail')
        if branch not in ('retail', 'ptr'):
            raise ValueError('仅支持正式服和测试服')
        resource = params.get('resource', 'items')
        identity = number(params, 'id', maximum=9223372036854775807)
        if resource == 'items':
            result = self.items(params, branch, identity)
        elif resource == 'talents':
            result = self.talents(params, branch, identity)
        else:
            raise ValueError('查询类型无效')
        if result is None:
            return JsonResponse({'success': False, 'error': '当前分支或筛选范围内没有该记录'}, status=404)
        return JsonResponse({'success': True, 'resource': resource, 'branch': branch, **result})

    def items(self, params, branch, identity):
        variants = WowItemVariantSnapshot.objects.annotate(_branch=Coalesce(
            NullIf(KeyTextTransform('data_branch', 'metadata'), Value(''), output_field=CharField()),
            Case(When(metadata__ptr_preview=True, then=Value('ptr')), default=F('data_branch'), output_field=CharField()),
            output_field=CharField()
        )).filter(_branch=branch, batch_key=F('season__gear_batch_key')).annotate(_quality=Coalesce(
            Cast(KeyTextTransform.from_lookup('metadata__difficulty_drop__quality'), IntegerField()),
            Cast(KeyTextTransform('quality', KeyTextTransform(branch,
                KeyTextTransform('branch_display', 'item__metadata'))), IntegerField()),
            F('item__quality'), output_field=IntegerField()))
        season = number(params, 'season')
        if season:
            variants = variants.filter(season_id=season)
        for field in ('variant_type', 'upgrade_track'):
            if params.get(field):
                variants = variants.filter(**{field: params[field]})
        low, high = number(params, 'min_level', maximum=10000), number(params, 'max_level', maximum=10000)
        if low is not None and high is not None and low > high:
            raise ValueError('最低装等不能高于最高装等')
        if low is not None:
            variants = variants.filter(item_level__gte=low)
        if high is not None:
            variants = variants.filter(item_level__lte=high)
        quality = number(params, 'quality', maximum=7)
        if quality is not None:
            variants = variants.filter(_quality=quality)
        rows = WowItemSnapshot.objects.annotate(_has_variant=Exists(variants.filter(item_id=OuterRef('pk'))))
        fields = {'_name': ('name', CharField()), '_name_zh': ('name_zh', CharField()),
                  '_slot': ('slot_key', CharField())}
        for alias, (field, output) in fields.items():
            path = KeyTextTransform.from_lookup(f'metadata__branch_display__{branch}__{field}')
            # Cast the completed fallback, not just its JSON arm: MySQL can
            # otherwise give COALESCE mixed implicit collations (NONE), which
            # fails in LIKE, slot equality and the language REGEXP filters.
            rows = rows.annotate(**{alias: Cast(Coalesce(path, F(field), output_field=output), output)})
        rows = rows.filter(_has_variant=True)
        if params.get('slot'):
            rows = rows.filter(_slot=params['slot'])
        rows = localized_filter(rows, '_name_zh', params.get('language', ''))
        q = params.get('q', '').strip()[:150]
        if q:
            condition = Q(_name__icontains=q) | Q(_name_zh__icontains=q) | Q(simc_token__icontains=q)
            if q.isdecimal() and int(q) <= 9223372036854775807:
                condition |= Q(item_id=int(q))
            rows = rows.filter(condition)
        if identity is not None:
            item = rows.filter(item_id=identity).first()
            if not item:
                return None
            selected, pagination = page_rows(variants.filter(item=item).select_related('season').order_by('-item_level', 'pk'), params)
            display = branch_item(item, branch)
            result = self.item_summary(display)
            effects, status = resolve_effects(item, branch=branch, allow_reference=True)
            result.update(description=display.description_zh or display.description,
                          effects=effects, effects_status=status, source=display.source,
                          updated_at=item.updated_at.isoformat(), branch=branch)
            entries = []
            for row in selected:
                row.item = item
                effects, status = resolve_effects(item, row, branch=branch,
                    known_empty=_native_journal_variant(row) and not row.effects_data)
                entries.append({'id': row.pk, 'item_level': row.item_level,
                    'kind': row.get_variant_type_display(), 'track': TRACKS.get(row.upgrade_track, row.upgrade_track),
                    'rank': row.track_rank, 'season': row.season.season_name, 'stats': row.stats_json,
                    'quality': QUALITY_CN.get(row._quality, '未知'),
                    'effects': effects, 'effects_status': status, 'sources': row.source_json,
                    'source_build': row.game_build, 'updated_at': row.updated_at.isoformat()})
            return {'record': result, 'entries': entries, 'pagination': pagination}
        selected, pagination = page_rows(rows.order_by('item_id'), params)
        counts = {}
        for row in variants.filter(item_id__in=[item.pk for item in selected]).order_by().values('item_id', '_quality').annotate(
                count=Count('pk'), minimum=Min('item_level'), maximum=Max('item_level')):
            bucket = counts.setdefault(row['item_id'], {'count': 0, 'minimum': row['minimum'], 'maximum': row['maximum'], 'qualities': []})
            bucket['count'] += row['count']
            bucket['minimum'] = min(bucket['minimum'], row['minimum'])
            bucket['maximum'] = max(bucket['maximum'], row['maximum'])
            bucket['qualities'].append(QUALITY_CN.get(row['_quality'], '未知'))
        records = []
        for item in selected:
            record = self.item_summary(branch_item(item, branch))
            record.update({key: value for key, value in counts.get(item.pk, {}).items() if key != 'item_id'})
            record['quality'] = ' / '.join(sorted(set(record.pop('qualities', [])))) or record['quality']
            record['id'] = item.item_id
            records.append(record)
        return {'records': records, 'pagination': pagination, 'facets': {
            'seasons': [{'value': str(row.pk), 'label': row.season_name} for row in SeasonMeta.objects.exclude(gear_batch_key='').order_by('-pk')],
            'slots': options(SLOTS), 'qualities': options({key: QUALITY_CN[key] for key in range(8)}),
            'types': options(dict(WowItemVariantSnapshot.TYPE_CHOICES)), 'tracks': options(TRACKS)}}

    @staticmethod
    def item_summary(item):
        icon_name = item_cdn_icon(item)
        icon_url = wow_icon_oss_url(icon_name, 'large') if icon_name else ''
        return {'id': item.item_id, 'name': item.name_zh or item.name or f'物品 {item.item_id}',
                'name_zh': item.name_zh, 'name_en': item.name, 'slot': SLOTS.get(item.slot_key, item.slot_key),
                'quality': QUALITY_CN.get(item.quality, '未知'), 'translated': bool(re.search(CJK, item.name_zh)),
                'icon': item.icon, 'icon_url': icon_url,
                'icon_source': f'统一图标服务：{urlsplit(icon_url).netloc}' if icon_url else '尚无已解析的图标地址'}

    def talents(self, params, branch, identity):
        try:
            version = TalentVersionResolver.resolve(version_key=branch)
        except ValueError:
            version = None
        rows = (WowTalentNodeMetadata.objects.filter(talent_version=version) if version else WowTalentNodeMetadata.objects.none()).exclude(node_id__isnull=True)
        spell = Coalesce(NullIf(F('display_spell_id'), Value(0)), Case(
            When(tree_type='hero_anchor', spell_id=F('node_id'), then=Value(None)),
            default=NullIf(F('spell_id'), Value(0)), output_field=BigIntegerField()))
        rows = rows.annotate(_spell=spell).annotate(
            identity=Coalesce(F('_spell'), F('node_id'), output_field=BigIntegerField()),
            identity_kind=Case(When(_spell__isnull=True, then=Value('node')), default=Value('spell'), output_field=CharField()))
        for field in ('class_name', 'spec_name', 'tree_type'):
            if params.get(field):
                rows = rows.filter(**{field: params[field]})
        snapshots = WowSpellSnapshot.objects.filter(branch=spell_branch(branch), locale='zhCN')
        status = params.get('status', '')
        if status in ('ready', 'partial'):
            rows = rows.filter(_spell__in=snapshots.filter(display_data__status=status).values('spell_id'))
        elif status == 'unbuilt':
            rows = rows.filter(Q(_spell__isnull=True) | ~Q(_spell__in=snapshots.filter(display_data__has_key='default').values('spell_id')))
        elif status:
            raise ValueError('合成状态无效')
        q = params.get('q', '').strip()[:150]
        if q:
            found = snapshots.filter(Q(display_data__default__name_zh__icontains=q) |
                Q(display_data__default__name_en__icontains=q) | Q(display_data__default__description_zh__icontains=q))
            condition = (Q(name__icontains=q) | Q(name_zh__icontains=q) | Q(description_zh__icontains=q) | Q(description__icontains=q)
                         | Q(_spell__in=found.values('spell_id')))
            if q.isdecimal() and int(q) <= 9223372036854775807:
                condition |= Q(identity=int(q)) | Q(node_id=int(q)) | Q(talent_id=int(q))
            rows = rows.filter(condition)
        provider = TalentMetadataProvider(talent_version=version)
        if identity is not None:
            kind = params.get('identity_kind', 'spell')
            if kind not in ('spell', 'node'):
                raise ValueError('天赋身份类型无效')
            selected, pagination = page_rows(rows.filter(identity=identity, identity_kind=kind).order_by('class_name', 'spec_name', 'node_id'), params)
            if not selected:
                return None
            entries = [self.talent_summary(row, provider) for row in provider._prime_display(selected)]
            central = SpellDisplayReader(branch).get(identity) if kind == 'spell' else {}
            return {'record': {**entries[0], 'central': central, 'branch': branch,
                               'source_build': getattr(version, 'current_build', '')}, 'entries': entries, 'pagination': pagination}
        groups, pagination = page_rows(rows.order_by().values('identity', 'identity_kind').annotate(
            representative=Min('pk'), count=Count('pk')).order_by('identity_kind', 'identity'), params)
        representatives = list(WowTalentNodeMetadata.objects.filter(pk__in=[row['representative'] for row in groups]))
        by_pk = {row.pk: self.talent_summary(row, provider) for row in provider._prime_display(representatives)}
        records = [{**by_pk[row['representative']], 'id': row['identity'], 'identity_kind': row['identity_kind'], 'count': row['count']}
                   for row in groups if row['representative'] in by_pk]
        return {'records': records, 'pagination': pagination, 'facets': {
            'classes': options(CLASS_CN), 'specs': options({spec: SPEC_CN.get(spec, spec) for spec in
                (CLASS_SPEC_MAP.get(params.get('class_name'), []) if params.get('class_name') else SPEC_CN)}),
            'trees': options(TREES)}}

    @staticmethod
    def talent_summary(row, provider):
        display = provider._as_dict(row)
        return {'id': node_spell_id(row) or row.node_id, 'node_id': row.node_id,
                'talent_id': row.talent_id, 'spell_id': node_spell_id(row),
                'name': display['name'] or f'天赋 {row.node_id}', 'name_zh': display['name_zh'],
                'name_en': row.name, 'description': display['description_zh'] or display['description'],
                'description_zh': display['description_zh'], 'tree': TREES.get(row.tree_type, row.tree_type),
                'tree_type': row.tree_type,
                'class_name': CLASS_CN.get(row.class_name, row.class_name), 'spec_name': SPEC_CN.get(row.spec_name, row.spec_name),
                'source': row.source, 'updated_at': row.last_updated.isoformat(), **icon_payload(display['icon'])}
