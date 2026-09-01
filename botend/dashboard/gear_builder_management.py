"""Dashboard 职业配装管理接口。"""

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.models import GearBuilderShareLink, GearBuilderUserLoadout


def _iso_datetime(value):
    if not value:
        return None
    return timezone.localtime(value).isoformat()


def _user_payload(user):
    return {
        'id': user.id,
        'username': user.get_username(),
    }


class DashboardGearBuilderManagementAPIView(DashboardPermissionRequiredMixin, View):
    """按需管理线上配装字符串和短链接映射，不在列表中传输编码正文。"""

    dashboard_permission = 'gear-builder.manage'
    resource_models = {
        'loadouts': GearBuilderUserLoadout,
        'shares': GearBuilderShareLink,
    }

    def _resource_model(self, resource):
        return self.resource_models.get(str(resource or '').strip())

    def _not_found(self):
        return JsonResponse({'success': False, 'error': '管理记录不存在'}, status=404)

    @staticmethod
    def _summary():
        return {
            'loadouts': GearBuilderUserLoadout.objects.count(),
            'shares': GearBuilderShareLink.objects.count(),
            'active_shares': GearBuilderShareLink.objects.filter(is_active=True).count(),
        }

    @staticmethod
    def _loadout_payload(row, include_code=False):
        payload = {
            'id': row.id,
            'resource': 'loadouts',
            'user': _user_payload(row.user),
            'name': row.name,
            'class_name': row.class_name,
            'spec_name': row.spec_name,
            'batch_key': row.batch_key,
            'state_hash': row.state_hash,
            'created_at': _iso_datetime(row.created_at),
            'updated_at': _iso_datetime(row.updated_at),
        }
        if include_code:
            payload['code'] = row.encoded_state
        return payload

    @staticmethod
    def _share_payload(row, include_code=False):
        payload = {
            'id': row.id,
            'resource': 'shares',
            'user': _user_payload(row.user),
            'token': row.token,
            'short_path': f'/g/{row.token}/',
            'class_name': row.class_name,
            'spec_name': row.spec_name,
            'batch_key': row.batch_key,
            'state_hash': row.state_hash,
            'access_count': row.access_count,
            'is_active': row.is_active,
            'last_accessed_at': _iso_datetime(row.last_accessed_at),
            'created_at': _iso_datetime(row.created_at),
            'updated_at': _iso_datetime(row.updated_at),
        }
        if include_code:
            payload['code'] = row.encoded_state
        return payload

    def _serialize(self, resource, row, include_code=False):
        if resource == 'loadouts':
            return self._loadout_payload(row, include_code=include_code)
        return self._share_payload(row, include_code=include_code)

    def get(self, request, resource, object_id=None):
        model = self._resource_model(resource)
        if model is None:
            return self._not_found()
        queryset = model.objects.select_related('user')
        if object_id is not None:
            row = queryset.filter(pk=object_id).first()
            if row is None:
                return self._not_found()
            return JsonResponse({
                'success': True,
                'record': self._serialize(resource, row, include_code=True),
            })

        query = str(request.GET.get('q') or '').strip()[:100]
        if query:
            common = (
                Q(user__username__icontains=query)
                | Q(class_name__icontains=query)
                | Q(spec_name__icontains=query)
                | Q(batch_key__icontains=query)
            )
            if resource == 'loadouts':
                queryset = queryset.filter(common | Q(name__icontains=query))
            else:
                queryset = queryset.filter(common | Q(token__icontains=query))
        if resource == 'shares':
            active = str(request.GET.get('active') or 'all').strip().lower()
            if active == 'active':
                queryset = queryset.filter(is_active=True)
            elif active == 'inactive':
                queryset = queryset.filter(is_active=False)

        try:
            page_size = min(100, max(10, int(request.GET.get('page_size') or 25)))
            page_number = max(1, int(request.GET.get('page') or 1))
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': '分页参数无效'}, status=400)
        paginator = Paginator(queryset, page_size)
        try:
            page = paginator.page(page_number)
        except EmptyPage:
            page = paginator.page(paginator.num_pages or 1)
        return JsonResponse({
            'success': True,
            'resource': resource,
            'records': [self._serialize(resource, row) for row in page.object_list],
            'pagination': {
                'page': page.number,
                'page_size': page_size,
                'pages': paginator.num_pages,
                'total': paginator.count,
            },
            'summary': self._summary(),
        })

    def delete(self, request, resource, object_id=None):
        model = self._resource_model(resource)
        if model is None or object_id is None:
            return self._not_found()
        row = model.objects.filter(pk=object_id).first()
        if row is None:
            return self._not_found()
        if resource == 'shares':
            if row.is_active:
                row.is_active = False
                row.save(update_fields=('is_active', 'updated_at'))
            return JsonResponse({'success': True, 'disabled': True})
        row.delete()
        return JsonResponse({'success': True, 'deleted': True})
