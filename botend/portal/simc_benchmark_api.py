"""Public, read-only SimC benchmark endpoints for the Portal."""

from django.http import JsonResponse
from django.views import View

from botend.models import SimcBenchmarkPanel
from botend.services.simc_benchmark_execution import serialize_incremental_panel_results


_NOT_READY = {'status': 'not_ready', 'results': {'coordinates': []}}


def _public_result_payload(panel):
    results = serialize_incremental_panel_results(panel)
    coordinates = results.get('coordinates', [])
    return {
        'status': 'ready' if coordinates else 'not_ready',
        'panel': {
            'slug': panel.slug,
            'name': panel.name,
            'description': panel.description,
        },
        'results': {'coordinates': coordinates},
    }


class PortalSimcBenchmarkPanelListAPIView(View):
    """List public panels without exposing mutable benchmark configuration."""

    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        panels = []
        queryset = SimcBenchmarkPanel.objects.filter(
            is_active=True, is_public=True,
        ).order_by('name', 'id')
        for panel in queryset:
            payload = _public_result_payload(panel)
            panels.append({
                'slug': panel.slug,
                'name': panel.name,
                'description': panel.description,
                'status': payload['status'],
            })
        return JsonResponse({'status': 'ready', 'panels': panels})


class PortalSimcBenchmarkPanelDetailAPIView(View):
    """Return an immediate projection for a publicly visible benchmark panel."""

    http_method_names = ['get', 'head', 'options']

    def get(self, request, slug):
        panel = SimcBenchmarkPanel.objects.filter(
            is_active=True, is_public=True, slug=slug,
        ).first()
        return JsonResponse(_public_result_payload(panel) if panel else _NOT_READY)
