"""共享游戏名称管理，写入实体原表。"""
from django.http import JsonResponse
import json
from django.views import View
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections
from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.models import WowTalentVersion, WowTalentNodeMetadata
from django.db.models import BigIntegerField, BinaryField, Case, Count, F, Func, Min, Q, When
from django.db.models.functions import Coalesce
from botend.services.wow_localization import (NameEditConflict, _record, _reference_identity,
                                               normalize_guide_reference, version_for, version_label,
                                               write_name)


class _Binary(Func):
    """MySQL BINARY expression: group translated labels byte-for-byte, not by CI collation."""
    function = 'BINARY'
    template = 'BINARY %(expressions)s'
    output_field = BinaryField()


class WowLocalizationAPI(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'tools.wow-localization'

    def dispatch(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
        except NameEditConflict as exc:
            response = JsonResponse({'error': str(exc)}, status=409)
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            response = JsonResponse({'error': str(exc)}, status=400)
        except IntegrityError:
            response = JsonResponse({'error': '名称记录已变化，请刷新后重试'}, status=409)
        response['Cache-Control'] = 'private, no-store'
        return response

    def get(self, request):
        versions = sorted({version_label(v, v.key) for v in WowTalentVersion.objects.all()})
        version = version_label(None, request.GET.get('version', ''))
        query, kind = request.GET.get('q', '').strip(), request.GET.get('kind', '')
        rows = WowTalentNodeMetadata.all_objects.exclude(name_zh='').select_related('talent_version')
        if version:
            selected = version_for(version)
            rows = rows.filter(talent_version=selected) if selected else rows.none()
        if kind:
            rows = rows.filter(name_kind=kind)
        condition = None
        if query:
            condition = Q(name__icontains=query) | Q(name_zh__icontains=query)
            if query.isdecimal():
                identity = int(query)
                aliases = [pk for pk, values in rows.filter(localization_only=False).values_list('pk', 'reference_aliases') if identity in values]
                condition |= (Q(reference_id=identity) | Q(talent_id=identity) | Q(node_id=identity) |
                              Q(spell_id=identity) | Q(display_spell_id=identity) | Q(pk__in=aliases))
        page = max(1, min(int(request.GET.get('page', 1)), 10000))
        # 天赋表按职业、专精和树位置保存结构行，同一个可编辑名称会有多行。
        # 管理页面只聚合显示完全相同的名称事实；同编号的不同选择项必须保留为独立行。
        rows = rows.annotate(effective_identity=Case(
            When(localization_only=True, then=F('reference_id')),
            default=Coalesce('talent_id', 'node_id', 'spell_id'),
            output_field=BigIntegerField(),
        ))
        exact_fields = ['name', 'name_zh', 'icon', 'localization_evidence']
        if connections[rows.db].vendor == 'mysql':
            rows = rows.annotate(**{'exact_' + field: _Binary(field) for field in exact_fields})
            exact_fields = ['exact_' + field for field in exact_fields]
        grouped = rows.values(
            'talent_version_id', 'name_kind', 'localization_only', 'effective_identity', *exact_fields,
        ).annotate(representative_pk=Min('pk'), duplicate_count=Count('pk'))
        if condition is not None:
            grouped = grouped.annotate(matching_pk=Min('pk', filter=condition),
                                       match_count=Count('pk', filter=condition)).filter(match_count__gt=0)
        grouped = grouped.order_by(
            'talent_version_id', 'name_kind', 'localization_only', 'effective_identity', 'representative_pk')
        total = grouped.count()
        page_slice = slice((page-1)*100, page*100)
        for attempt in range(2):
            page_groups = list(grouped[page_slice])
            selected_pks = [group.get('matching_pk') or group['representative_pk'] for group in page_groups]
            representatives = {
                obj.pk: obj for obj in WowTalentNodeMetadata.all_objects.filter(
                    pk__in=selected_pks).select_related('talent_version')
            }
            if all(pk in representatives for pk in selected_pks):
                break
        records = []
        for group, selected_pk in zip(page_groups, selected_pks):
            obj = representatives.get(selected_pk)
            if obj is None:  # A concurrent metadata refresh removed it twice; omit the transient row.
                continue
            record = _record(obj, obj.name_kind, version_label(obj.talent_version, ''), group['effective_identity'])
            record['duplicate_count'] = group['duplicate_count']
            record['identifiers'] = list(dict.fromkeys(filter(None, [
                group['effective_identity'], obj.node_id, obj.spell_id, obj.display_spell_id, *obj.reference_aliases,
            ])))
            records.append(record)
        return JsonResponse(dict(records=records, total=total, page=page, versions=versions))

    def post(self, request):
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError('名称资料必须是对象')
        record_pk = data.get('record_pk')
        if record_pk is None and data.get('create') is not True:
            raise NameEditConflict('页面版本已过期，请刷新后重试')
        if data.get('create') is True:
            is_natural_term = data.get('object_id') in (None, '') and data.get('kind') in ('phrase', 'macro')
            if not is_natural_term:
                data = normalize_guide_reference(data)
        else:
            if isinstance(record_pk, bool) or not isinstance(record_pk, int) or record_pk < 1:
                raise ValidationError('名称记录编号无效')
            target = WowTalentNodeMetadata.all_objects.select_related('talent_version').filter(pk=record_pk).first()
            if target is None:
                raise NameEditConflict('名称记录已变化，请刷新后重试')
            identity = (target.reference_id if target.localization_only else
                        target.talent_id or target.node_id or target.spell_id)
            submitted_identity = _reference_identity(data.get('object_id'))
            if submitted_identity != identity:
                raise NameEditConflict('名称记录已变化，请刷新后重试')
            data = {
                **data,
                'game_version': target.talent_version.key,
                'kind': target.name_kind,
                'object_id': identity,
                'name_en': target.name,
                'icon': target.icon,
                'evidence': target.localization_evidence,
            }
        record, _ = write_name(data, overwrite=True, preserve_blank=record_pk is None,
                               target_pk=record_pk, target_state=data.get('edit_state'))
        return JsonResponse({key: record[key] for key in (
            'id', 'kind', 'object_id', 'game_version', 'name_en', 'icon')})
