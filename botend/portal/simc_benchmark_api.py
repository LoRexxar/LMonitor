"""Public, read-only SimC benchmark endpoints for the Portal."""

from django.http import JsonResponse
from django.views import View

from botend.models import SimcBenchmarkPanel
from botend.services.simc_benchmark_execution import serialize_public_execution


_NOT_READY = {'status': 'not_ready', 'execution': None}


class PortalSimcBenchmarkPanelListAPIView(View):
    """List public panels without exposing mutable benchmark configuration."""

    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        panels = []
        queryset = SimcBenchmarkPanel.objects.filter(
            is_active=True, is_public=True,
        ).order_by('name', 'id')
        for panel in queryset:
            public = serialize_public_execution(panel)
            panels.append({
                'slug': panel.slug,
                'name': panel.name,
                'description': panel.description,
                'status': 'ready' if public.get('status') == 'ready' else 'not_ready',
            })
        return JsonResponse({'status': 'ready', 'panels': panels})


class PortalSimcBenchmarkPanelDetailAPIView(View):
    """Return a sealed aggregate by slug, including unlisted active panels."""

    http_method_names = ['get', 'head', 'options']

    def get(self, request, slug):
        panel = SimcBenchmarkPanel.objects.filter(
            is_active=True, slug=slug,
        ).first()
        payload = serialize_public_execution(panel) if panel is not None else _NOT_READY
        if payload.get('status') not in {'ready', 'not_ready'}:
            payload = _NOT_READY
        return JsonResponse(payload)
