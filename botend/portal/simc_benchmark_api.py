"""Public, read-only SimC benchmark endpoints for the Portal."""
from django.db.models import Count, Exists, Max, OuterRef
from django.http import JsonResponse
from django.views import View

from botend.models import (
    SimcBenchmarkPanel, SimcBenchmarkProfile, SimcBenchmarkScenario,
)
from botend.services.simc_benchmark_execution import serialize_incremental_panel_results


_NOT_READY = {'status': 'not_ready', 'results': {'coordinates': []}}


def _public_result_payload(panel, *, coordinate_filter=None, scenario_filter=None):
    if scenario_filter is not None:
        results = serialize_incremental_panel_results(
            panel,
            scenario_filter=scenario_filter,
            include_coordinate_options=True,
        )
    elif coordinate_filter is None:
        results = serialize_incremental_panel_results(panel)
    else:
        results = serialize_incremental_panel_results(
            panel,
            coordinate_filter=coordinate_filter,
            include_coordinate_options=True,
        )
    coordinates = results.get('coordinates', [])
    payload = {
        'status': 'ready' if coordinates else 'not_ready',
        'panel': {
            'id': panel.id,
            'slug': panel.slug,
            'name': panel.name,
            'description': panel.description,
        },
        'results': {'coordinates': coordinates},
    }
    if isinstance(results.get('coordinate_options'), list):
        payload['results']['coordinate_options'] = results['coordinate_options']
    if scenario_filter is not None:
        payload['result_view'] = 'spec_comparison'
    return payload

class PortalSimcBenchmarkPanelListAPIView(View):
    """List public panels without exposing mutable benchmark configuration."""

    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        panels = []
        queryset = SimcBenchmarkPanel.objects.filter(
            is_active=True, is_public=True,
        ).annotate(
            has_enabled_scenario=Exists(
                SimcBenchmarkScenario.objects.filter(
                    panel_id=OuterRef('pk'), is_enabled=True,
                )
            ),
            has_enabled_profile=Exists(
                SimcBenchmarkProfile.objects.filter(
                    panel_spec__panel_id=OuterRef('pk'),
                    panel_spec__is_enabled=True,
                    is_enabled=True,
                )
            ),
            result_count=Count('executions__cases__results', distinct=True),
            result_updated_at=Max('executions__cases__results__created_at'),
        ).order_by('name', 'id')
        for panel in queryset:
            is_ready = (
                panel.has_enabled_scenario
                and panel.has_enabled_profile
            )
            panels.append({
                'id': panel.id,
                'slug': panel.slug,
                'name': panel.name,
                'description': panel.description,
                'status': 'ready' if is_ready else 'not_ready',
                'result_count': panel.result_count,
                'result_updated_at': (
                    panel.result_updated_at.isoformat()
                    if panel.result_updated_at else None
                ),
            })
        return JsonResponse({'status': 'ready', 'panels': panels})


class PortalSimcBenchmarkPanelDetailAPIView(View):
    """Return an immediate projection for a publicly visible benchmark panel."""

    http_method_names = ['get', 'head', 'options']

    def get(self, request, panel_id=None, slug=None):
        lookup = {'id': panel_id} if panel_id is not None else {'slug': slug}
        panel = SimcBenchmarkPanel.objects.filter(is_active=True, **lookup).first()
        if panel is None:
            return JsonResponse(_NOT_READY)
        coordinate_filter = None
        scenario_filter = None
        if request.GET.get('selected') == '1':
            if panel.candidates.filter(is_enabled=True).exists():
                coordinate_filter = {
                    'spec_key': request.GET.get('spec', ''),
                    'profile_key': request.GET.get('profile', ''),
                    'scenario_key': request.GET.get('scenario', ''),
                }
            else:
                scenario_filter = request.GET.get('scenario', '')
        return JsonResponse(_public_result_payload(
            panel,
            coordinate_filter=coordinate_filter,
            scenario_filter=scenario_filter,
        ))
