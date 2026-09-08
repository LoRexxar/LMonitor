"""共享游戏名称管理，写入实体原表。"""
from django.http import JsonResponse
import json
from django.views import View
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.models import WowTalentVersion, WowTalentNodeMetadata
from django.db.models import Q
from botend.services.wow_localization import write_name, version_label, version_for, _record


class WowLocalizationAPI(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'tools.wow-localization'

    def dispatch(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
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
        if query:
            condition = Q(name__icontains=query) | Q(name_zh__icontains=query)
            if query.isdecimal():
                identity = int(query)
                aliases = [pk for pk, values in rows.filter(localization_only=False).values_list('pk', 'reference_aliases') if identity in values]
                condition |= Q(reference_id=identity) | Q(talent_id=identity) | Q(node_id=identity) | Q(pk__in=aliases)
            rows = rows.filter(condition)
        page = max(1, min(int(request.GET.get('page', 1)), 10000))
        total = rows.count()
        records = []
        for obj in rows.order_by('talent_version_id', 'name_kind', 'localization_only', 'reference_id', 'id')[(page-1)*100:page*100]:
            identity = obj.reference_id if obj.localization_only else obj.talent_id or obj.node_id or obj.spell_id
            records.append(_record(obj, obj.name_kind, version_label(obj.talent_version, ''), identity))
        return JsonResponse(dict(records=records, total=total, page=page, versions=versions))

    def post(self, request):
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError('名称资料必须是对象')
        if not data.get('evidence'):
            raise ValueError('请填写名称核对依据')
        record, _ = write_name(data, overwrite=True)
        return JsonResponse({'id': record['id']})
