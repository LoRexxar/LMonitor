"""公开配置模拟的独立后台，所有写入均使用受权限和 CSRF 保护的 POST。"""

from django import forms
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from botend.dashboard.permissions import DashboardPermissionRequiredMixin, effective_dashboard_permissions, permission_catalog
from botend.models import SimcBackendBinary, SimcContentTemplate, SimcReferenceSimulation, SimcReferenceSimulationConfig, SimcReferencePublication, SimcReferenceSimulationSettings, SimcProfile, SimcTalentString, SimcApl, SimcTaskArtifact
from botend.services.simc_reference_simulations import catalog, enqueue_configs, initialize_configs, validate_config, validate_settings, snapshot
from botend.services.simc_task_service import _compute_content_hash
from botend.services.simc_reference_management import public_combination_keys, scheme_results, with_report
from botend.services.simc_reference_details import RESOURCE_LABELS, resource_is_shared, resource_preview


def _positive_id(value):
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) <= 0:
        raise Http404('记录不存在')
    return int(value)


class SimcReferenceResourcePreviewView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'frontend.simulations'
    http_method_names = ['get', 'head', 'options']

    def get(self, request, spec_key, kind, resource_id):
        if kind not in RESOURCE_LABELS:
            raise Http404('资料类型不存在')
        config = get_object_or_404(SimcReferenceSimulationConfig,
                                  pk=_positive_id(request.GET.get('config')), spec_key=spec_key)
        ids = {key: None if request.GET.get(key) == '0' else _positive_id(request.GET.get(key))
               for key in RESOURCE_LABELS}
        if ids[kind] != resource_id:
            raise Http404('资料不属于该组合')
        combination = tuple(ids.values())
        is_public = combination in public_combination_keys(config)
        if not is_public and not SimcReferenceSimulation.objects.filter(
            config=config, **{key + '_id': value for key, value in ids.items()},
        ).exists():
            raise Http404('配置组合不存在')
        resources = {key: model.objects.filter(pk=ids[key]).first() for key, model in (
            ('profile', SimcProfile), ('talent', SimcTalentString), ('apl', SimcApl),
        )}
        resource = resources[kind]
        if not resource_is_shared(resource, kind, config):
            raise Http404('该共享资料已不可用')
        spec = catalog(public_only=False).get(spec_key)
        if not spec:
            raise Http404('专精不存在')
        response = JsonResponse(resource_preview(kind, resource, spec, config, is_public=is_public))
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


class SimcReferenceRankingsView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'frontend.simulations'
    http_method_names = ['get', 'head', 'options']

    def get(self, request, config_id=None, signature=None, scope='overall'):
        from botend.dashboard.dashboard import DashboardView
        from botend.services.simc_reference_rankings import ranking_page_context, ranking_entry
        if config_id is None:
            entry = ranking_entry(request.GET.get('config'))
            if entry:
                response = redirect('simc_reference_rankings', *entry)
                response['Cache-Control'] = 'private, no-store'
                response['X-Robots-Tag'] = 'noindex, nofollow'
                return response
            board = {'empty_ranking': True, 'module_section': 'rankings', 'admin_ranking': True,
                     'module_reference_url': reverse('simc_reference_results'),
                     'module_rankings_url': reverse('simc_reference_rankings_entry')}
        else:
            config = get_object_or_404(SimcReferenceSimulationConfig.objects.select_related('settings__backend', 'settings__template'), pk=config_id)
            board = ranking_page_context(config, signature, scope, admin=True)
            if board is None:
                raise Http404('当前配置已更新或不再公开，请返回结果管理查看排名')
        dashboard = DashboardView()
        dashboard.setup(request)
        context = dashboard.get_context_data(title='后台', page_name='frontend-rankings', include_stats=False)
        context.update(board)
        context.update(dashboard_permissions=sorted(effective_dashboard_permissions(request.user)),
                       dashboard_permission_catalog=permission_catalog(), dashboard_default_section='frontend-rankings')
        response = render(request, 'dashboard/simc_reference_rankings.html', context)
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


class ConfigForm(forms.ModelForm):
    target_values = forms.CharField(label='目标数量', help_text='以逗号分隔，例如 1,5')

    class Meta:
        model = SimcReferenceSimulationSettings
        fields = ['backend', 'template', 'duration', 'iterations', 'is_enabled']
        labels = {'backend': '模拟后端', 'template': '输入模板', 'duration': '时长（秒）',
                  'iterations': '迭代次数', 'is_enabled': '启用模拟结果维护与展示'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['backend'].queryset = SimcBackendBinary.objects.filter(is_active=True)
        self.fields['template'].queryset = SimcContentTemplate.objects.filter(
            is_active=True, is_selectable=True, owner_user_id__isnull=True,
            spec__in=('default', 'all', '*'),
        )
        self.fields['target_values'].initial = ','.join(map(str, self.instance.targets))

    def clean_target_values(self):
        try:
            values = [int(part.strip()) for part in self.cleaned_data['target_values'].replace('，', ',').split(',')]
        except ValueError:
            raise forms.ValidationError('请输入逗号分隔的目标数量，例如 1,5')
        if not 1 <= len(values) <= 10 or len(set(values)) != len(values) or any(n < 1 or n > 20 for n in values):
            raise forms.ValidationError('目标须为 1–20 的不重复整数，最多 10 个')
        self.instance.targets = values
        return values

    def clean(self):
        data = super().clean()
        for field in self.Meta.fields:
            if field in data:
                setattr(self.instance, field, data[field])
        if data.get('is_enabled') and not self.errors:
            try:
                validate_settings(self.instance)
            except ValueError as exc:
                raise forms.ValidationError(str(exc))
        return data


class SimcReferenceSettingsView(DashboardPermissionRequiredMixin, View):
    """每个游戏分支只有一份模拟条件，保存不派发任务。"""
    dashboard_permission = 'frontend.simulations'
    http_method_names = ['get', 'post', 'head', 'options']

    def branch(self, request):
        branch = request.POST.get('branch', 'retail') if request.method == 'POST' else request.GET.get('branch', 'retail')
        if branch not in ('retail', 'ptr'):
            raise Http404('游戏分支不存在')
        return branch

    def render_page(self, request, branch, form, *, status=200):
        from botend.dashboard.dashboard import DashboardView
        dashboard = DashboardView()
        dashboard.setup(request)
        context = dashboard.get_context_data(title='后台', page_name='frontend-settings', include_stats=False)
        context.update(dashboard_permissions=sorted(effective_dashboard_permissions(request.user)),
                       dashboard_permission_catalog=permission_catalog(), dashboard_default_section='frontend-settings',
                       management_title='模拟配置', management_description='统一管理所有专精的模拟条件，正式服与 PTR 分开设置。',
                       branch=branch, settings_form=form)
        response = render(request, 'dashboard/simc_reference_settings.html', context, status=status)
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    def get(self, request):
        branch = self.branch(request)
        settings = SimcReferenceSimulationSettings.objects.filter(use_ptr=branch == 'ptr').first()
        return self.render_page(request, branch, ConfigForm(instance=settings or SimcReferenceSimulationSettings(use_ptr=branch == 'ptr')))

    def post(self, request):
        branch = self.branch(request)
        if request.POST.get('action') != 'save':
            raise Http404('操作不存在')
        with transaction.atomic():
            settings = SimcReferenceSimulationSettings.objects.select_for_update().filter(use_ptr=branch == 'ptr').first()
            form = ConfigForm(request.POST, instance=settings or SimcReferenceSimulationSettings(use_ptr=branch == 'ptr'))
            if form.is_valid():
                form.save()
                return redirect(reverse('simc_reference_settings') + f'?branch={branch}&saved=conditions')
        return self.render_page(request, branch, form, status=400)


class PublicationForm(forms.ModelForm):
    class Meta:
        model = SimcReferencePublication
        fields = ['is_published', 'profiles', 'talents', 'apls']
        labels = {'is_published': '启用该专精的前台展示', 'profiles': '装备配置', 'talents': '天赋方案', 'apls': '技能优先级（APL）'}
        widgets = {key: forms.CheckboxSelectMultiple for key in ('profiles', 'talents', 'apls')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        candidates = catalog(public_only=False).get(self.instance.spec_key, {})
        for kind, model in (('profiles', SimcProfile), ('talents', SimcTalentString), ('apls', SimcApl)):
            self.fields[kind].queryset = model.objects.filter(pk__in=[r['id'] for r in candidates.get(kind, [])]).order_by('name', 'pk')
            self.fields[kind].label_from_instance = lambda obj: obj.name


class SimcReferenceManagementView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'frontend.simulations'
    http_method_names = ['get', 'post', 'head', 'options']
    view_mode = None

    def get(self, request, **kwargs):
        return self.render_page(request)

    def render_page(self, request, *, form=None, publication_form=None, result=None, status=200):
        directory = catalog(public_only=False)
        public_directory = catalog()
        configs = list(SimcReferenceSimulationConfig.objects.select_related('settings__backend', 'settings__template').order_by('spec_key', 'use_ptr'))
        spec_key = self.kwargs.get('spec_key')
        spec_groups = {}
        if self.view_mode in ('results', 'publication'):
            from botend.constants.wow import CLASS_CN
            for key in dict.fromkeys(row.spec_key for row in configs):
                spec = directory.get(key, {})
                group = CLASS_CN.get(spec.get('class_name'), '其他职业')
                spec_groups.setdefault(group, []).append({
                    'key': key, 'label': spec.get('label', key), 'icon': spec.get('icon'),
                })
        if spec_key:
            configs = [row for row in configs if row.spec_key == spec_key]
            if not configs:
                raise Http404('专精不存在')
        for config in configs:
            spec = directory.get(config.spec_key, {})
            config.label = spec.get('label', config.spec_key)
            public = public_directory.get(config.spec_key, {})
            config.combination_count = sum(row['use_ptr'] == config.use_ptr for row in public.get('profiles', [])) * len(public.get('talents', []))
            config.public_count = sum(len(public.get(kind, [])) for kind in ('profiles', 'talents', 'apls'))
            config.issue = ''
            try:
                validate_config(config)
            except ValueError as exc:
                config.issue = str(exc)
        selected_id = str(form.instance.pk) if form is not None else (
            (request.POST.get('config') or request.GET.get('config')) if request.method == 'POST' else request.GET.get('config')
        )
        selected = next((row for row in configs if str(row.pk) == selected_id), configs[0] if configs else None)
        if self.view_mode == 'results':
            if not spec_key:
                selected = None
            elif selected_id and not any(str(row.pk) == selected_id for row in configs):
                raise Http404('该配置不属于当前专精')
        if self.view_mode == 'publication':
            primary_configs = {}
            for config in configs:
                primary_configs.setdefault(config.spec_key, config)
            if selected:
                primary_configs[selected.spec_key] = selected
            for specs in spec_groups.values():
                for spec in specs:
                    spec['url'] = reverse('simc_reference_publication') + f"?config={primary_configs[spec['key']].pk}"
        if publication_form is None and selected and self.view_mode == 'publication':
            publication = SimcReferencePublication.objects.filter(spec_key=selected.spec_key).first()
            publication_form = PublicationForm(instance=publication or SimcReferencePublication(spec_key=selected.spec_key))
        history_scheme, page, scheme_page = None, None, None
        if selected and self.view_mode == 'results':
            if request.GET.get('scheme'):
                history_scheme = get_object_or_404(SimcReferenceSimulation, pk=_positive_id(request.GET['scheme']), config=selected)
                simulations = with_report(SimcReferenceSimulation.objects.filter(
                    config=selected, profile_id=history_scheme.profile_id,
                    talent_id=history_scheme.talent_id, apl_id=history_scheme.apl_id,
                )).select_related('profile', 'talent', 'apl', 'config__settings__backend', 'config__settings__template').order_by('-pk')
                page = Paginator(simulations, 40).get_page(request.GET.get('page'))
                for row in page:
                    try:
                        targets = row.snapshot['simulation_params']['desired_targets']
                        current = snapshot(row.config, row.profile, row.talent, row.apl, targets)
                        row.is_stale = targets not in row.config.targets or _compute_content_hash(current) != row.signature
                    except (ValueError, KeyError):
                        row.is_stale = True
            else:
                scheme_page = scheme_results(selected, request.GET.get('page'))
        from botend.dashboard.dashboard import DashboardView
        dashboard = DashboardView()
        dashboard.setup(request)
        section = f'frontend-{self.view_mode}'
        context = dashboard.get_context_data(title='后台', page_name=section, include_stats=False)
        context.update({
            'dashboard_permissions': sorted(effective_dashboard_permissions(request.user)),
            'dashboard_permission_catalog': permission_catalog(),
            'dashboard_default_section': section,
            'view_mode': self.view_mode, 'management_title': self.management_title,
            'management_description': self.management_description, 'management_url': request.path,
            'spec_groups': spec_groups.items(), 'spec_count': sum(len(rows) for rows in spec_groups.values()),
            'configs': configs, 'selected': selected, 'form': form, 'result': result, 'page': page,
            'publication_form': publication_form, 'scheme_page': scheme_page, 'history_scheme': history_scheme,
            'module_section': 'publication' if self.view_mode == 'publication' else 'reference', 'admin_ranking': True,
            'module_reference_url': (reverse('simc_reference_results') + (f'?config={selected.pk}' if selected else '')) if self.view_mode == 'publication' else request.get_full_path(),
            'module_rankings_url': reverse('simc_reference_rankings_entry') + (f'?config={selected.pk}' if selected else ''),
        })
        response = render(request, self.template_name, context, status=status)
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    def post(self, request, **kwargs):
        action = request.POST.get('action')
        if action not in self.allowed_actions:
            return self.render_page(request, result={'errors': ['该操作不属于当前管理页面']}, status=400)
        result_config = None
        if self.view_mode == 'results':
            if not self.kwargs.get('spec_key'):
                return self.render_page(request, result={'errors': ['请先选择专精，再管理该专精的模拟结果']}, status=400)
            scoped = SimcReferenceSimulationConfig.objects.filter(spec_key=self.kwargs['spec_key']).order_by('use_ptr')
            config_id = request.POST.get('config') or request.GET.get('config')
            result_config = get_object_or_404(scoped, pk=_positive_id(config_id)) if config_id else scoped.first()
            if result_config is None:
                raise Http404('专精不存在')
        if action == 'sync':
            initialize_configs()
            return redirect(self.url_name)
        if action == 'publish':
            config = get_object_or_404(SimcReferenceSimulationConfig, pk=_positive_id(request.POST.get('config')))
            with transaction.atomic():
                publication, _ = SimcReferencePublication.objects.get_or_create(spec_key=config.spec_key)
                publication = SimcReferencePublication.objects.select_for_update().get(pk=publication.pk)
                publication_form = PublicationForm(request.POST, instance=publication)
                if publication_form.is_valid():
                    publication_form.save()
                    return redirect(f'{request.path}?config={config.pk}&saved=publication')
            return self.render_page(request, publication_form=publication_form, status=400)
        if action in ('generate', 'rerun', 'rerun_scheme', 'start_scheme'):
            configs = SimcReferenceSimulationConfig.objects.filter(pk=result_config.pk)
            combination = None
            if action == 'start_scheme':
                combination = tuple(_positive_id(request.POST.get(kind)) for kind in ('profile', 'talent', 'apl'))
                if combination not in public_combination_keys(result_config):
                    raise Http404('该组合不属于当前专精的公开配置')
            if action == 'rerun_scheme':
                scheme = get_object_or_404(SimcReferenceSimulation, pk=_positive_id(request.POST.get('scheme')), config=result_config)
                configs = configs.filter(pk=scheme.config_id)
                combination = (scheme.profile_id, scheme.talent_id, scheme.apl_id)
            if request.POST.get('config'):
                config = get_object_or_404(configs, pk=_positive_id(request.POST['config']))
                configs = configs.filter(pk=config.pk)
            result = enqueue_configs(list(configs.values_list('pk', flat=True)), request.user.pk, force=action in ('rerun', 'rerun_scheme'), combination=combination)
            return self.render_page(request, result=result)
        if action == 'cancel':
            simulation = get_object_or_404(SimcReferenceSimulation, pk=_positive_id(request.POST.get('simulation')), config=result_config)
            SimcReferenceSimulation.objects.filter(pk=simulation.pk, status='queued').update(status='cancelled')
            return redirect(f'{request.path}?config={result_config.pk}&scheme={simulation.pk}')
        return self.render_page(request, result={'errors': ['不支持的操作']}, status=400)


class SimcReferencePublicationView(SimcReferenceManagementView):
    view_mode = 'publication'
    url_name = 'simc_reference_publication'
    template_name = 'dashboard/simc_reference_publication.html'
    management_title = '公开配置管理'
    management_description = '按专精指定可在前台展示的装备、天赋和 APL。'
    allowed_actions = {'sync', 'publish'}


class SimcReferenceResultsView(SimcReferenceManagementView):
    view_mode = 'results'
    url_name = 'simc_reference_results'
    template_name = 'dashboard/simc_reference_results.html'
    management_title = '模拟结果管理'
    management_description = '查看已公开方案的 DPS 结果、运行历史与报告，模拟条件由全局配置统一管理。'
    allowed_actions = {'generate', 'rerun', 'rerun_scheme', 'start_scheme', 'cancel'}

    def get(self, request, **kwargs):
        if not kwargs.get('spec_key'):
            if request.GET.get('config'):
                config = get_object_or_404(SimcReferenceSimulationConfig, pk=_positive_id(request.GET['config']))
            else:
                config = SimcReferenceSimulationConfig.objects.order_by('spec_key', 'use_ptr').first()
            if config:
                query = request.GET.urlencode()
                return redirect(reverse('simc_reference_spec_results', kwargs={'spec_key': config.spec_key}) + (f'?{query}' if query else ''))
        return self.render_page(request)


class SimcReferenceEntryView(DashboardPermissionRequiredMixin, View):
    """兼容旧入口；新页面不再使用标签参数切换功能。"""
    dashboard_permission = 'frontend.simulations'
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        name = 'simc_reference_results' if request.GET.get('tab') == 'results' else 'simc_reference_publication'
        params = request.GET.copy()
        params.pop('tab', None)
        query = params.urlencode()
        return redirect(reverse(name) + (f'?{query}' if query else ''))


class SimcReferenceReportView(DashboardPermissionRequiredMixin, View):
    """只开放本模块结果关联的报告，无需获得通用 SimC 历史任务权限。"""
    dashboard_permission = 'frontend.simulations'
    http_method_names = ['get', 'head', 'options']

    def get(self, request, result_id):
        result = get_object_or_404(SimcReferenceSimulation, pk=result_id, task__isnull=False)
        artifact = SimcTaskArtifact.objects.filter(
            task_id=result.task_id, run__task_id=result.task_id, run__status='completed',
            artifact_type='html_report',
        ).order_by('-created_at', '-pk').first()
        if not artifact:
            raise Http404('报告尚未生成或已不可用')
        from botend.dashboard.api import SimcArtifactPreviewAPIView
        return SimcArtifactPreviewAPIView().get(request, object_id=artifact.pk)
