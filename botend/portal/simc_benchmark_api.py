"""Public, read-only SimC benchmark endpoints for the Portal."""
import json

from django.db.models import Count, Exists, Max, OuterRef
from django.http import JsonResponse
from django.views import View

from botend.models import (
    SimcBenchmarkPanel, SimcBenchmarkProfile, SimcBenchmarkScenario,
    SimcBenchmarkSpec,
)
from botend.services.simc_benchmark_execution import (
    serialize_incremental_panel_results, serialize_panel_apl_ranking_results,
)


_NOT_READY = {'status': 'not_ready', 'results': {'coordinates': []}}


def _ranking_not_ready(reason):
    return {'status': 'not_ready', 'reason': reason, 'rankings': []}


def _canonical(value):
    return json.dumps(value or {}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _baseline_projection(panel, scenario_key):
    projection = serialize_incremental_panel_results(panel, scenario_filter=scenario_key)
    return [row for row in projection.get('coordinates', [])
            if row.get('scenario_key') == scenario_key]


def _projected_ranking_row(coordinate):
    baseline = next((row for row in coordinate.get('candidates', [])
                     if row.get('key') == 'baseline'), None)
    audit = coordinate.get('audit') if isinstance(coordinate.get('audit'), dict) else {}
    required = ('profile_identity', 'apl_identity', 'template_identity')
    if baseline is None or not all(audit.get(key) for key in required):
        return None
    if not isinstance(audit.get('simulation_params'), dict):
        return None
    labels = coordinate.get('labels') or {}
    return {
        'spec_key': coordinate.get('spec_key'),
        'spec_label': labels.get('spec'),
        'profile_key': coordinate.get('profile_key'),
        'profile_label': labels.get('profile'),
        'scenario_label': labels.get('scenario'),
        'apl_key': audit['apl_identity'],
        'apl_label': audit.get('apl_label') or audit['apl_identity'],
        'dps': float(baseline['dps']),
        'source_result_id': baseline.get('source_result_id'),
        'resource_versions': {
            'profile': audit['profile_identity'], 'template': audit['template_identity'],
            'apl': audit['apl_identity'], 'backend': audit['backend_version'],
        },
        'simulation_params': audit['simulation_params'],
    }


def get_portal_apl_ranking(panel, spec_key, scenario_key, profile_key=None):
    """Rank only baseline rows selected by the authoritative incremental projection."""
    if not panel.is_active or not panel.is_public:
        return _ranking_not_ready('panel_not_public')
    configured = SimcBenchmarkSpec.objects.filter(
        panel=panel, spec_key=spec_key, is_enabled=True,
    ).first()
    scenario = SimcBenchmarkScenario.objects.filter(
        panel=panel, key=scenario_key, is_enabled=True,
    ).first()
    selected_profile = None
    requested_profile_key = profile_key
    if configured is not None:
        profiles = configured.profiles.filter(is_enabled=True).order_by('display_order', 'id')
        if profile_key:
            try:
                profile_id = int(profile_key)
            except (TypeError, ValueError):
                profile_id = None
            selected_profile = profiles.filter(profile_id=profile_id).first() if profile_id is not None else None
        else:
            selected_profile = profiles.first()
    if configured is None or scenario is None or selected_profile is None:
        return _ranking_not_ready('dimension_not_configured')
    profile_key = str(selected_profile.profile_id)
    rankings = serialize_panel_apl_ranking_results(
        panel, spec_key=spec_key, scenario_key=scenario_key,
    )
    if rankings is None:
        return _ranking_not_ready('incomplete_frozen_identity')
    if requested_profile_key is not None:
        rankings = [row for row in rankings if str(row.get('profile_key')) == profile_key]
    else:
        # Older frozen projections used labels such as ``raid`` or ``standard``
        # instead of the configured SimcProfile primary key. Keep those readable
        # for the default view; an explicit profile selection remains strict.
        rankings = [
            row for row in rankings
            if str(row.get('profile_key')) == profile_key
            or not str(row.get('profile_key', '')).isdigit()
        ]
    if not rankings:
        return _ranking_not_ready('no_comparable_baseline_results')
    identities = {(row['resource_versions']['profile'],
                   row['resource_versions']['template'],
                   row['resource_versions']['backend'],
                   _canonical(row['simulation_params'])) for row in rankings}
    if len(identities) != 1:
        return _ranking_not_ready('no_comparable_baseline_results')
    rankings.sort(key=lambda row: (-row['dps'], row['apl_key']))
    return {'status': 'ready', 'panel_id': panel.id, 'scenario_key': scenario_key,
            'spec_key': spec_key, 'profile_key': profile_key, 'rankings': rankings}


def get_portal_spec_ranking(panel, scenario_key):
    """Rank enabled standard coordinates already selected by the projection."""
    if not panel.is_active or not panel.is_public:
        return _ranking_not_ready('panel_not_public')
    scenario = SimcBenchmarkScenario.objects.filter(
        panel=panel, key=scenario_key, is_enabled=True,
    ).first()
    if scenario is None:
        return _ranking_not_ready('dimension_not_configured')
    enabled_keys = set(SimcBenchmarkSpec.objects.filter(
        panel=panel, is_enabled=True,
    ).values_list('spec_key', flat=True))
    coordinates = [
        row for row in _baseline_projection(panel, scenario_key)
        if row.get('spec_key') in enabled_keys
        and any(candidate.get('key') == 'baseline'
                for candidate in row.get('candidates', []))
    ]
    # A panel may keep several profiles for one spec. The cross-spec view
    # ranks one stable standard coordinate per spec: the first enabled profile
    # in the panel's configured display order.
    preferred_profiles = {}
    for profile in SimcBenchmarkProfile.objects.filter(
        panel_spec__panel=panel, panel_spec__is_enabled=True, is_enabled=True,
    ).select_related('panel_spec').order_by(
        'panel_spec__display_order', 'panel_spec_id', 'display_order', 'id',
    ):
        preferred_profiles.setdefault(profile.panel_spec.spec_key, str(profile.profile_id))
    coordinates = [
        row for row in coordinates
        if preferred_profiles.get(row.get('spec_key')) == row.get('profile_key')
    ]
    projected = [_projected_ranking_row(row) for row in coordinates]
    if coordinates and any(row is None for row in projected):
        return _ranking_not_ready('incomplete_frozen_identity')
    rankings = [row for row in projected if row is not None]
    if not rankings:
        return _ranking_not_ready('no_comparable_baseline_results')
    shared = {(row['resource_versions']['template'], row['resource_versions']['backend'],
               _canonical(row['simulation_params'])) for row in rankings}
    if len(shared) != 1:
        return _ranking_not_ready('no_comparable_baseline_results')
    rankings.sort(key=lambda row: (-row['dps'], row['spec_key']))
    return {'status': 'ready', 'panel_id': panel.id, 'scenario_key': scenario_key,
            'rankings': rankings}


def get_portal_baseline_results(panel, spec_key):
    """Return every published baseline coordinate for one specialization."""
    if not panel.is_active or not panel.is_public:
        return _ranking_not_ready('panel_not_public')
    if not SimcBenchmarkSpec.objects.filter(
        panel=panel, spec_key=spec_key, is_enabled=True,
    ).exists():
        return _ranking_not_ready('dimension_not_configured')
    projection = serialize_incremental_panel_results(panel, spec_filter=spec_key)
    rows = []
    for coordinate in projection.get('coordinates', []):
        if coordinate.get('spec_key') != spec_key:
            continue
        baseline = next(
            (candidate for candidate in coordinate.get('candidates', [])
             if candidate.get('key') == 'baseline'), None,
        )
        if baseline is None or not coordinate.get('audit'):
            continue
        row = dict(coordinate)
        row['dps'] = baseline.get('dps')
        row['source_result_id'] = baseline.get('source_result_id')
        rows.append(row)
    if not rows:
        return _ranking_not_ready('no_comparable_baseline_results')
    rows.sort(key=lambda row: (-float(row.get('dps') or 0),
                               str(row.get('scenario_key') or ''),
                               str(row.get('profile_key') or '')))
    for index, row in enumerate(rows, 1):
        row['rank'] = index
    return {'status': 'ready', 'panel_id': panel.id, 'spec_key': spec_key,
            'rankings': rows}


def _find_public_panel(request):
    panel_id = request.GET.get('panel_id')
    slug = request.GET.get('panel')
    if not panel_id and not slug:
        return None
    lookup = {'id': panel_id} if panel_id else {'slug': slug}
    return SimcBenchmarkPanel.objects.filter(is_active=True, is_public=True, **lookup).first()


class _PortalRankingAPIView(View):
    http_method_names = ['get', 'head', 'options']

    def panel(self, request):
        panel = _find_public_panel(request)
        if panel is None:
            return None, JsonResponse(_ranking_not_ready('panel_not_public'))
        return panel, None


class PortalSimcAplRankingAPIView(_PortalRankingAPIView):
    def get(self, request):
        panel, error = self.panel(request)
        if error:
            return error
        return JsonResponse(get_portal_apl_ranking(
            panel, request.GET.get('spec', ''), request.GET.get('scenario', ''),
            request.GET.get('profile') or None,
        ))


class PortalSimcBaselineResultsAPIView(_PortalRankingAPIView):
    def get(self, request):
        panel, error = self.panel(request)
        if error:
            return error
        return JsonResponse(get_portal_baseline_results(
            panel, request.GET.get('spec', ''),
        ))


class PortalSimcSpecRankingAPIView(_PortalRankingAPIView):
    def get(self, request):
        panel, error = self.panel(request)
        if error:
            return error
        return JsonResponse(get_portal_spec_ranking(
            panel, request.GET.get('scenario', ''),
        ))


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
        # is_public controls Portal discovery only. Existing direct-link reads
        # remain available for active panels by product contract.
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
